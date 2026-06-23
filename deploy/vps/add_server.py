"""Provision a fresh Debian VPS as an AegisVPN node and register it on the
main bot in a single shot. Idempotent enough to re-run if a step fails.

Steps:
  1. SSH to the new VPS, install docker if absent.
  2. Upload the entire agent source tree + docker-compose.yml.
  3. Write vpn.env with the desired Reality settings, `docker compose up xray agent`.
  4. Read the auto-generated agent.env (Reality keys, agent token).
  5. SSH to the main VPS, upload the register helper, insert/update the
     server row in the bot's SQLite, push every active user UUID to the new
     agent's /client/add endpoint, then `docker compose restart xray` on the
     new node so xray reloads with all clients.

Defaults assume the standard Reality-TCP layout (XRAY_TCP_PORT=0), so the
single inbound on XRAY_PORT uses the *_TCP key pair from agent.env. That's
the layout `register_external_server.py` expects.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import stat
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "paramiko is required for deploy/vps/add_server.py. Install it first, "
        "for example: python -m pip install paramiko"
    ) from exc


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
COMPOSE_FILE = ROOT / "deploy" / "vps" / "docker-compose.yml"
MAIN_REGISTER_SCRIPT = ROOT / ".codex_tmp" / "register_external_server.py"
MAIN_RESYNC_SCRIPT = ROOT / ".codex_tmp" / "resync_server_by_id.py"
REMOTE_SETUP_SCRIPT = "/root/aegis/setup_server.sh"
REMOTE_REGISTER_SCRIPT = "/root/aegis/register_external_server.py"
REMOTE_RESYNC_SCRIPT = "/root/aegis/resync_server_by_id.py"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Provision + register a new Aegis VPN server.")
    p.add_argument("--main-host", required=True, help="Main VPS holding the bot SQLite")
    p.add_argument("--main-password", required=True)
    p.add_argument("--new-host", required=True, help="Fresh VPS IP/hostname")
    p.add_argument("--new-password", required=True)
    p.add_argument("--server-name", required=True,
                   help="Display name with flag emoji at the start, e.g. '🇯🇵 Japan | Tokyo'")
    p.add_argument("--server-domain", required=True,
                   help="IP/hostname the client connects to (use bare IP, not sslip.io)")
    p.add_argument("--agent-url", help="Defaults to http://<new-host>:8444")
    p.add_argument("--xray-port", default="443")
    p.add_argument("--reality-dest", required=True,
                   help="REALITY dest, e.g. csc.fi:443 — a geo-matched, China-reachable "
                        "TLS1.3 site. NO default (gateway.icloud.com is implausible on a "
                        "datacenter IP and gets РКН-probed).")
    p.add_argument("--reality-server-name", required=True,
                   help="REALITY serverName / SNI, e.g. csc.fi — the geo-SNI for this "
                        "node's location. No default.")
    p.add_argument("--xray-network", default="xhttp", choices=["xhttp", "tcp"],
                   help="Transport protocol for the primary inbound (default: xhttp)")
    p.add_argument("--no-warp", action="store_true",
                   help="Skip registering a per-location Cloudflare WARP account "
                        "(by default each node gets its own, used to route AI "
                        "domains around VPS-range blocks).")
    p.add_argument("--no-mtproxy", action="store_true",
                   help="Skip setting up MTProxy (Telegram proxy) on this node.")
    return p.parse_args()


# --- SSH plumbing ----------------------------------------------------------
#
# Fresh DataForest / 4vps VPSes (and the main VPS) often rate-limit fresh SSH
# connects after a burst: sshd / fail2ban will silently drop the banner for the
# next ~30-60s. We work around this two ways:
#   1) Open exactly ONE SSH session per host and hold it open for every step
#      that talks to that host. The same SFTP channel is reused for all
#      uploads — no fresh banners per file.
#   2) connect() retries with exponential backoff so the first auth survives
#      whatever rate-limit window was triggered before we ran.

_dir_cache: dict[int, set[str]] = {}  # sftp_id -> set of paths we've already ensured


def connect(host: str, password: str, attempts: int = 6) -> paramiko.SSHClient:
    """Open an SSH session as root, retrying past rate-limit drops with
    exponential backoff."""
    delay = 5.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host, username="root", password=password,
                timeout=60, banner_timeout=60, auth_timeout=60,
            )
            return client
        except (paramiko.SSHException, EOFError, OSError) as exc:
            last = exc
            try: client.close()
            except Exception: pass
            if attempt == attempts:
                break
            print(f"    ssh retry {attempt}/{attempts - 1} after {exc} (sleep {delay:.0f}s)")
            time.sleep(delay)
            delay = min(delay * 1.7, 45.0)
    raise SystemExit(f"could not reach {host} after {attempts} attempts: {last}")


def exec_command(client: paramiko.SSHClient, command: str, timeout: int = 120) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def run_or_die(client: paramiko.SSHClient, command: str, label: str, timeout: int = 120) -> str:
    code, out, err = exec_command(client, command, timeout=timeout)
    if code != 0:
        raise SystemExit(f"{label} failed (exit {code}):\n{(err or out).strip()[-1000:]}")
    return out


def get_sftp(client: paramiko.SSHClient) -> paramiko.SFTPClient:
    """Cached SFTP channel per SSH client. Avoids opening a fresh subsystem
    per file — that's a round-trip we don't need."""
    sftp = getattr(client, "_aegis_sftp", None)
    if sftp is None:
        sftp = client.open_sftp()
        client._aegis_sftp = sftp  # type: ignore[attr-defined]
    return sftp


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    cache = _dir_cache.setdefault(id(sftp), set())
    if remote_dir in cache:
        return
    parts: list[str] = []
    current = remote_dir
    while current not in ("", "/"):
        parts.append(current)
        current = posixpath.dirname(current)
    for path in reversed(parts):
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)
        cache.add(path)


def upload_file(client: paramiko.SSHClient, local_path: Path, remote_path: str) -> None:
    sftp = get_sftp(client)
    ensure_remote_dir(sftp, posixpath.dirname(remote_path))
    sftp.put(str(local_path), remote_path)
    if local_path.stat().st_mode & stat.S_IXUSR:
        sftp.chmod(remote_path, 0o755)


def write_remote_script(client: paramiko.SSHClient, path: str, content: str) -> None:
    sftp = get_sftp(client)
    ensure_remote_dir(sftp, posixpath.dirname(path))
    with sftp.file(path, "w") as remote_file:
        remote_file.write(content)
    sftp.chmod(path, 0o755)


# --- agent payload ---------------------------------------------------------

def upload_agent(client: paramiko.SSHClient) -> None:
    """Upload everything the agent's Dockerfile build context needs.

    Previously this had a hand-curated subset (main/config/models only) and
    we'd discover the missing imports the hard way after `docker compose up`
    crashlooped. Now we upload every .py under app/ plus all build files."""
    base_files = [
        AGENT_DIR / "Dockerfile",
        AGENT_DIR / "entrypoint.sh",
        AGENT_DIR / "pyproject.toml",
        AGENT_DIR / "uv.lock",
        AGENT_DIR / "template.json",
    ]
    app_files = sorted((AGENT_DIR / "app").glob("*.py"))
    for f in base_files + app_files:
        remote = "/root/aegis/agent/" + f.relative_to(AGENT_DIR).as_posix()
        upload_file(client, f, remote)


# --- docker bootstrap ------------------------------------------------------

DOCKER_BOOTSTRAP = """#!/bin/sh
# Install docker if not present. The official get.docker.com installer is the
# safe choice across Debian 11/12/13 and Ubuntu — distro packages lag and
# don't ship `docker compose` v2 (we need the plugin, not docker-compose).
set -eu
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "docker already installed: $(docker --version)"
    exit 0
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates >/dev/null
curl -fsSL https://get.docker.com | sh >/tmp/get-docker.log 2>&1 || {
    echo "docker install failed; tail of /tmp/get-docker.log:"
    tail -20 /tmp/get-docker.log
    exit 1
}
systemctl enable --now docker >/dev/null 2>&1 || true
docker --version
"""


# Registers a fresh, per-location Cloudflare WARP (free) account on the node
# and appends its creds to agent.env. entrypoint's ensure_warp() then wires the
# WireGuard outbound + the AI-domain routing rule on the next restart. Each node
# gets its OWN account (Cloudflare rate-limits one identity across many IPs, and
# we don't want every location egressing as the same WARP peer). Idempotent:
# bails out if agent.env already carries a WARP_SECRET_KEY. Best-effort — a
# failed registration must not abort provisioning, so the caller ignores errors.
WARP_REGISTER_SCRIPT = r"""#!/bin/sh
set -eu
ENV=/root/aegis/deploy/vps/data/vpn/agent.env
if grep -q '^WARP_SECRET_KEY=' "$ENV" 2>/dev/null; then
    echo "warp already registered"; exit 0
fi
if ! command -v wg >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq wireguard-tools curl >/dev/null 2>&1 || true
fi
command -v wg >/dev/null 2>&1 || { echo "wg missing; skip warp"; exit 0; }
PRIV=$(wg genkey)
PUB=$(printf '%s' "$PRIV" | wg pubkey)
TOS=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
RESP=$(curl -s --max-time 25 -X POST 'https://api.cloudflareclient.com/v0a2158/reg' \
    -H 'User-Agent: okhttp/3.12.1' -H 'CF-Client-Version: a-6.30-3596' \
    -H 'Content-Type: application/json' \
    -d "{\"key\":\"$PUB\",\"install_id\":\"\",\"fcm_token\":\"\",\"tos\":\"$TOS\",\"model\":\"PC\",\"type\":\"Linux\",\"locale\":\"en_US\"}") || {
        echo "warp reg request failed; skip"; exit 0; }
# RESP goes via env, NOT a pipe: python3's stdin is already taken by the
# heredoc that carries the program itself — piping data in too would make
# json read an empty/exhausted stdin.
RESP="$RESP" PRIV="$PRIV" python3 - "$ENV" <<'PYEOF'
import base64, json, os, sys
env_path = sys.argv[1]
try:
    resp = json.loads(os.environ["RESP"])
    # Cloudflare returns the config at the top level (no "result" wrapper) on
    # the reg endpoint; tolerate both shapes.
    result = resp.get("result", resp)
    cfg = result["config"]
    peer = cfg["peers"][0]
    v4 = cfg["interface"]["addresses"]["v4"]
    v6 = cfg["interface"]["addresses"]["v6"]
    client_id = cfg.get("client_id") or result.get("client_id")
    reserved = list(base64.b64decode(client_id))[:3]
except Exception as exc:
    print("warp parse failed:", exc); sys.exit(0)
fields = {
    "WARP_SECRET_KEY": os.environ["PRIV"],
    "WARP_ADDR_V4": v4 + "/32",
    "WARP_ADDR_V6": v6 + "/128",
    "WARP_PEER_PUBKEY": peer["public_key"],
    "WARP_ENDPOINT": "162.159.192.1:2408",
    "WARP_RESERVED": ",".join(str(b) for b in reserved),
    "WARP_MTU": "1280",
}
with open(env_path, "a", encoding="utf-8") as fh:
    for k, val in fields.items():
        fh.write(f"{k}={val}\n")
print("warp registered v4=%s v6=%s reserved=%s" % (v4, v6, reserved))
PYEOF
"""


# Host network tuning applied to every fresh node. BBR + fq is the single
# biggest throughput/latency win over the stock cubic + fq_codel on a long RTT
# path to RU clients; mtu_probing lets the kernel recover from PMTU black holes
# (common on mobile carriers); tcp_fastopen shaves a round-trip off repeat
# connects. Idempotent — safe to re-run.
NET_TUNING = """#!/bin/sh
set -eu
modprobe tcp_bbr 2>/dev/null || true
echo tcp_bbr > /etc/modules-load.d/aegis-bbr.conf
cat > /etc/sysctl.d/99-aegis-net.conf <<EOF
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.ipv4.tcp_mtu_probing=1
net.ipv4.tcp_fastopen=3
EOF
sysctl --system >/dev/null 2>&1 || true
echo "cc=$(sysctl -n net.ipv4.tcp_congestion_control) qdisc=$(sysctl -n net.core.default_qdisc)"
"""


def build_setup_script(args: argparse.Namespace) -> str:
    """Initial bring-up script: write vpn.env, start the vpn container, wait
    for entrypoint to materialise agent.env (Reality keys etc.)."""
    return f"""#!/bin/sh
set -eu
mkdir -p /root/aegis /root/aegis/deploy/vps /root/aegis/deploy/vps/data/vpn
cat > /root/aegis/deploy/vps/vpn.env <<'EOF'
XRAY_RUN_MODE=internal
XRAY_CONFIG_PATH=/data/xray-config.json
XRAY_PORT={args.xray_port}
XRAY_TCP_PORT=0
XRAY_NETWORK={args.xray_network}
REALITY_DEST={args.reality_dest}
REALITY_SERVER_NAME={args.reality_server_name}
HOST_IP={args.server_domain}
EOF
# Reset client state so re-provisioned nodes don't get duplicate clients
rm -f /root/aegis/deploy/vps/data/vpn/client_map.json
rm -f /root/aegis/deploy/vps/data/vpn/xray-config.json
cd /root/aegis/deploy/vps
docker compose up -d --build xray agent 2>&1 | tail -3
# Entrypoint generates agent.env on first run; wait until it exists.
for i in $(seq 1 30); do
    [ -f /root/aegis/deploy/vps/data/vpn/agent.env ] && break
    sleep 1
done
ls -la /root/aegis/deploy/vps/data/vpn/agent.env
"""


# --- parsing ---------------------------------------------------------------

def parse_env(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def pick_reality_keys(env: dict[str, str], xray_network: str = "xhttp") -> tuple[str, str]:
    """Agent generates TWO Reality keypairs: PUBLIC_KEY/SHORT_ID for the
    XHTTP inbound and *_TCP for the TCP inbound. Pick the right one based
    on which transport is active on XRAY_PORT."""
    if xray_network == "tcp":
        pk = env.get("PUBLIC_KEY_TCP") or env.get("PUBLIC_KEY")
        sid = env.get("SHORT_ID_TCP") or env.get("SHORT_ID")
    else:  # xhttp
        pk = env.get("PUBLIC_KEY") or env.get("PUBLIC_KEY_TCP")
        sid = env.get("SHORT_ID") or env.get("SHORT_ID_TCP")
    if not pk or not sid:
        raise SystemExit(f"agent.env missing Reality keys (network={xray_network})")
    return pk, sid


# --- name/flag parsing -----------------------------------------------------

def split_flag(name: str) -> tuple[str, str]:
    """Split a leading country-flag emoji from the server name.

    Country flags are two consecutive Regional Indicator letters (U+1F1E6–U+1F1FF).
    Returns (flag, display_name) — e.g. ('🇯🇵', 'Japan | Tokyo').
    If no flag is found the flag is empty and name is returned as-is."""
    s = name.strip()
    if (len(s) >= 2
            and 0x1F1E6 <= ord(s[0]) <= 0x1F1FF
            and 0x1F1E6 <= ord(s[1]) <= 0x1F1FF):
        return s[:2], s[2:].lstrip()
    return "", s


# --- env exporting ---------------------------------------------------------

def shell_quote(s: str) -> str:
    """Single-quote a string for /bin/sh. Handles emoji etc. without going
    through json.dumps (which would escape non-ASCII as \\uXXXX and the
    target shell would write literal backslash sequences into the DB)."""
    return "'" + s.replace("'", "'\\''") + "'"


# --- main flow -------------------------------------------------------------

def main() -> int:
    args = parse_args()
    server_flag, server_name = split_flag(args.server_name)
    agent_url = args.agent_url or f"http://{args.new_host}:8444"

    if not MAIN_REGISTER_SCRIPT.exists():
        raise SystemExit(f"Missing helper script: {MAIN_REGISTER_SCRIPT}")
    if not MAIN_RESYNC_SCRIPT.exists():
        raise SystemExit(f"Missing helper script: {MAIN_RESYNC_SCRIPT}")

    # Open ONE session per host and hold it for the entire flow. The naive
    # version reconnected 4× (twice per host); fresh VPS sshd / fail2ban
    # would rate-limit the second connect within ~30s and the script died.
    print(f"[1/6] connecting to new VPS {args.new_host}…")
    new_client = connect(args.new_host, args.new_password)
    main_client: paramiko.SSHClient | None = None
    server_id: int | None = None
    try:
        print("[2/6] installing docker + tuning host network (BBR/fq/PMTU)…")
        write_remote_script(new_client, "/root/aegis/install_docker.sh", DOCKER_BOOTSTRAP)
        out = run_or_die(new_client, "sh /root/aegis/install_docker.sh", "docker install", timeout=600)
        print("     " + out.strip().splitlines()[-1])
        write_remote_script(new_client, "/root/aegis/net_tuning.sh", NET_TUNING)
        # Best-effort: BBR may be unavailable on an exotic kernel; don't abort.
        code, tout, terr = exec_command(new_client, "sh /root/aegis/net_tuning.sh", timeout=60)
        print("     " + ((tout or terr).strip().splitlines() or ["(net tuning skipped)"])[-1])

        print("[3/6] uploading agent source + compose…")
        run_or_die(new_client, "mkdir -p /root/aegis/deploy/vps/data/vpn", "mkdir")
        upload_agent(new_client)
        upload_file(new_client, COMPOSE_FILE, "/root/aegis/deploy/vps/docker-compose.yml")

        print("[4/6] starting vpn container, waiting for Reality keys…")
        write_remote_script(new_client, REMOTE_SETUP_SCRIPT, build_setup_script(args))
        run_or_die(new_client, f"sh {REMOTE_SETUP_SCRIPT}", "vpn bring-up", timeout=600)

        out = run_or_die(new_client, "cat /root/aegis/deploy/vps/data/vpn/agent.env", "read agent.env")
        remote_env = parse_env(out)
        public_key, short_id = pick_reality_keys(remote_env, xray_network=args.xray_network)
        agent_token = remote_env.get("AGENT_TOKEN")
        if not agent_token:
            raise SystemExit("agent.env missing AGENT_TOKEN")
        print(f"     keys ok (pbk={public_key[:8]}… sid={short_id})")

        if not args.no_warp:
            print("     registering a per-location WARP account…")
            write_remote_script(new_client, "/root/aegis/warp_register.sh", WARP_REGISTER_SCRIPT)
            # Best-effort: a WARP failure must not abort the deploy. The final
            # restart picks up agent.env's WARP_* via entrypoint ensure_warp().
            code, wout, werr = exec_command(new_client, "sh /root/aegis/warp_register.sh", timeout=120)
            tail = (wout or werr).strip().splitlines()[-1:] or ["(no output)"]
            print("     " + tail[0])

        print(f"[5/6] registering server on main VPS {args.main_host}…")
        main_client = connect(args.main_host, args.main_password)
        upload_file(main_client, MAIN_REGISTER_SCRIPT, REMOTE_REGISTER_SCRIPT)
        # All env values shell-quoted so emoji flags / spaces in names work
        # unchanged in the DB (no json.dumps escapes).
        register_command = (
            f"SERVER_NAME={shell_quote(server_name)} "
            f"SERVER_FLAG={shell_quote(server_flag)} "
            f"SERVER_HOST={shell_quote(args.server_domain)} "
            f"SERVER_PORT={shell_quote(args.xray_port)} "
            f"PUBLIC_KEY={shell_quote(public_key)} "
            f"SHORT_ID={shell_quote(short_id)} "
            f"AGENT_URL={shell_quote(agent_url)} "
            f"AGENT_TOKEN={shell_quote(agent_token)} "
            f"python3 {REMOTE_REGISTER_SCRIPT}"
        )
        out = run_or_die(main_client, register_command, "register", timeout=300)
        print("     " + out.strip().splitlines()[-1])
        # `registered server_id=8, synced=N, total=M` — pull the id out.
        for line in out.splitlines():
            if "server_id=" in line:
                try:
                    server_id = int(line.split("server_id=")[1].split(",")[0])
                except (ValueError, IndexError):
                    pass
                break

        if server_id is None:
            print("[warn] couldn't parse server_id from register output; "
                  "resync + restart still needs to happen manually.")
            return 0

        print(f"[6/6] resync user UUIDs to server {server_id} + restart xray "
              "so the running core picks them up…")
        upload_file(main_client, MAIN_RESYNC_SCRIPT, REMOTE_RESYNC_SCRIPT)
        out = run_or_die(main_client, f"python3 {REMOTE_RESYNC_SCRIPT} {server_id}",
                         "resync", timeout=300)
        print("     " + out.strip())

        # The agent's /client/add path writes UUIDs to disk but doesn't always
        # hot-reload into the running xray (known bug). Restarting the xray
        # container forces it to re-read the config and accept all UUIDs.
        run_or_die(
            new_client,
            "cd /root/aegis/deploy/vps && docker compose restart xray 2>&1 | tail -2",
            "restart xray", timeout=120,
        )
        time.sleep(4)
        _, out, _ = exec_command(
            new_client,
            "docker exec aegis-vpn python3 -c "
            '"import json; '
            "c=json.load(open('/data/xray-config.json')); "
            "ib=c['inbounds'][0]; "
            "print('port=',ib['port'],'clients=',len(ib['settings']['clients']))\"",
            timeout=30,
        )
        print("     " + out.strip())

        if not args.no_mtproxy and server_id is not None:
            print("[+] setting up MTProxy (Telegram proxy) on port 80…")
            code, secret_out, _ = exec_command(
                new_client,
                "docker run --rm ghcr.io/9seconds/mtg:2 generate-secret google.com 2>/dev/null",
                timeout=60,
            )
            mtproxy_secret = secret_out.strip()
            if code == 0 and mtproxy_secret:
                run_or_die(
                    new_client,
                    f"cd /root/aegis/deploy/vps && "
                    f"docker compose --profile mtproxy run -d --name aegis-mtg "
                    f"--no-deps mtg run {mtproxy_secret} --bind 0.0.0.0:80 2>&1 | tail -2",
                    "start mtg", timeout=60,
                )
                # Store secret in bot DB
                update_cmd = (
                    f"docker exec aegis-bot python3 -c "
                    f"'import asyncio; from sqlalchemy import text; from src.core.database import async_session_maker\n"
                    f"exec(\"\"\"async def q():\\n"
                    f"    async with async_session_maker() as s:\\n"
                    f"        await s.execute(text(\\\"UPDATE servers SET mtproxy_secret=\\\\\\\"{mtproxy_secret}\\\\\\\" WHERE id={server_id}\\\"))\\n"
                    f"        await s.commit()\\n"
                    f"        print(\\\"ok\\\")\\n"
                    f"asyncio.run(q())\"\"\")'"
                )
                run_or_die(main_client, update_cmd, "save mtproxy_secret", timeout=30)
                print(f"     MTProxy ready — secret={mtproxy_secret[:8]}…")
                print(f"     Link: https://t.me/proxy?server={args.server_domain}&port=80&secret={mtproxy_secret}")
            else:
                print("     [warn] failed to generate MTProxy secret, skipping.")
    finally:
        if main_client is not None:
            try: main_client.close()
            except Exception: pass
        try: new_client.close()
        except Exception: pass

    print("\n✓ Done.")
    print(f"   Name: {server_flag} {server_name}")
    print(f"   Host: {args.server_domain}:{args.xray_port}")
    print(f"   Agent URL: {agent_url}")
    print(f"   Server ID in bot DB: {server_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
