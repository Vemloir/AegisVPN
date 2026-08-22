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

Defaults produce a node IDENTICAL to the rest of the fleet: an XHTTP inbound
on --xhttp-port (443) plus a TCP+VISION alt inbound on --tcp-port (2053), both
sharing the SINGLE reality keypair (entrypoint builds the tcp inbound from the
same PRIVATE_KEY/SHORT_ID). register_external_server.py writes tcp_port into
the DB so the bot offers the transport choice, and syncs every active device
UUID — not just the sub UUIDs — so the node never silently drops from a
device's subscription. Pass --tcp-port 0 for an xhttp-only node.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import posixpath
import secrets
import stat
import tempfile
import time
from ipaddress import IPv4Address, ip_address
from pathlib import Path

try:
    import paramiko
except ImportError:  # Allows offline unit tests of rendering helpers.
    paramiko = None  # type: ignore[assignment]

try:
    from .control_plane import (
        ensure_control_ca,
        issue_node_credentials,
        render_agent_firewall,
        render_node_control_env,
    )
except ImportError:  # Direct execution: python deploy/vps/add_server.py
    from control_plane import (
        ensure_control_ca,
        issue_node_credentials,
        render_agent_firewall,
        render_node_control_env,
    )


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
COMPOSE_FILE = ROOT / "deploy" / "vps" / "docker-compose.yml"
# Both live beside this script. They used to be pulled from an untracked scratch
# directory (.codex_tmp/, which is gitignored), so a fresh clone of the repo could
# not add a server at all — the deploy path depended on files that were never
# committed.
MAIN_REGISTER_SCRIPT = ROOT / "deploy" / "vps" / "register_external_server.py"
MAIN_RESYNC_SCRIPT = ROOT / "deploy" / "vps" / "resync_server_by_id.py"
REMOTE_SETUP_SCRIPT = "/root/aegis/setup_server.sh"
REMOTE_REGISTER_SCRIPT = "/root/aegis/register_external_server.py"
REMOTE_RESYNC_SCRIPT = "/root/aegis/resync_server_by_id.py"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Provision + register a new Aegis VPN server."
    )
    p.add_argument("--main-host", required=True, help="Main VPS holding the bot SQLite")
    p.add_argument("--main-username", default="root")
    p.add_argument(
        "--main-key-file",
        type=Path,
        help="SSH private key; otherwise prompt for password",
    )
    p.add_argument(
        "--main-host-fingerprint", help="Expected SHA256 SSH host-key fingerprint"
    )
    p.add_argument("--new-host", required=True, help="Fresh VPS IP/hostname")
    p.add_argument(
        "--new-key-file",
        type=Path,
        help="SSH private key; otherwise prompt for password",
    )
    p.add_argument(
        "--new-host-fingerprint", help="Expected SHA256 SSH host-key fingerprint"
    )
    p.add_argument(
        "--known-hosts",
        type=Path,
        default=Path("~/.ssh/known_hosts"),
        help="OpenSSH known_hosts file (default: ~/.ssh/known_hosts)",
    )
    p.add_argument(
        "--new-username",
        default="root",
        help="SSH user on the fresh VPS (default root). Non-root cloud users "
        "are supported through sudo; root/password SSH is never enabled.",
    )
    p.add_argument(
        "--server-name",
        required=True,
        help="Display name with flag emoji at the start, e.g. '🇯🇵 Japan | Tokyo'",
    )
    p.add_argument(
        "--server-domain",
        required=True,
        help="IP/hostname the client connects to (use bare IP, not sslip.io)",
    )
    p.add_argument("--agent-url", help="Defaults to http://<new-host>:8444")
    p.add_argument(
        "--control-url",
        action="append",
        required=True,
        help="Outbound node-control URL; repeat for failover. Must be HTTPS on "
        "standard TCP/443, for example https://control.example.com.",
    )
    p.add_argument(
        "--control-ca-dir",
        required=True,
        type=Path,
        help="Operator-only directory containing client-ca.crt/client-ca.key. "
        "It is created on first use and must be kept outside the repository.",
    )
    # Everything below is a field the bot reads about a node. They used to be
    # unreachable from this script, so every new node silently landed on the
    # schema default and differed from the nodes already in service.
    p.add_argument(
        "--country-code",
        metavar="XX",
        help="ISO 3166-1 alpha-2 (FI, SE, DE, JP, US). The website's globe "
        "draws a location only if this is set; the bot serves it either way.",
    )
    p.add_argument(
        "--subscription-group",
        default="safe",
        choices=["safe", "fast", "both"],
        help="Which subscription profile(s) show this node (default: safe). Match "
        "the nodes already in service — a mismatch here means the new location "
        "simply never appears for users on the other profile.",
    )
    p.add_argument(
        "--access-mode",
        default="public",
        choices=["public", "restricted"],
        help="public = everyone; restricted = only users with an explicit grant "
        "(default: public)",
    )
    p.add_argument(
        "--display-order",
        type=int,
        default=0,
        help="Sort position in the location list (default 0 = alphabetical fallback)",
    )
    p.add_argument(
        "--xhttp-port",
        "--xray-port",
        dest="xhttp_port",
        default="443",
        help="XHTTP (primary VLESS+REALITY) inbound port; registered as the "
        "server's connect port in the bot DB (default 443). On a fresh IP "
        "whose :443 proves unreliable (common on flagged US/datacenter "
        "ranges — the reality ClientHello gets reset), set an alt-HTTPS port "
        "like 2083: the same port class as --tcp-port 2053, which connects "
        "reliably. (--xray-port is a back-compat alias.)",
    )
    p.add_argument(
        "--reality-dest",
        required=True,
        help="REALITY dest, e.g. csc.fi:443 — a geo-matched, China-reachable "
        "TLS1.3 site. NO default (gateway.icloud.com is implausible on a "
        "datacenter IP and gets РКН-probed).",
    )
    p.add_argument(
        "--reality-server-name",
        required=True,
        help="REALITY serverName / SNI, e.g. csc.fi — the geo-SNI for this "
        "node's location. No default.",
    )
    p.add_argument(
        "--xray-network",
        default="xhttp",
        choices=["xhttp", "tcp"],
        help="Transport protocol for the primary inbound (default: xhttp)",
    )
    p.add_argument(
        "--tcp-port",
        default="2053",
        help="TCP+VISION (alt VLESS+REALITY) inbound port on the SAME "
        "reality keypair (flow=xtls-rprx-vision), registered as the "
        "server's tcp_port in the bot DB (default 2053). Every fleet "
        "node serves this second transport; setting it makes the bot "
        "offer a transport choice for the node. Free to pick any port "
        "(it must differ from --xhttp-port). Use '0' to disable "
        "(xhttp-only node, the odd one out).",
    )
    p.add_argument(
        "--no-warp",
        action="store_true",
        help="Skip registering a per-location Cloudflare WARP account "
        "(by default each node gets its own, used to route AI "
        "domains around VPS-range blocks).",
    )
    p.add_argument(
        "--no-mtproxy",
        action="store_true",
        help="Skip setting up MTProxy (Telegram proxy) on this node.",
    )
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


def _sha256_host_fingerprint(key) -> str:
    digest = (
        base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    )
    return f"SHA256:{digest}"


class PinnedHostKeyPolicy:
    """Accept an unknown host only when its out-of-band SHA-256 pin matches."""

    def __init__(self, expected_fingerprint: str):
        value = expected_fingerprint.strip()
        self.expected = value if value.startswith("SHA256:") else f"SHA256:{value}"

    def missing_host_key(self, client, hostname: str, key) -> None:
        actual = _sha256_host_fingerprint(key)
        if not secrets.compare_digest(actual, self.expected):
            raise HostKeyVerificationError(
                f"SSH host key fingerprint mismatch for {hostname}: expected {self.expected}, got {actual}"
            )
        if client is not None:
            client.get_host_keys().add(hostname, key.get_name(), key)


class HostKeyVerificationError(RuntimeError):
    pass


def connect(
    host: str,
    password: str | None,
    attempts: int = 6,
    username: str = "root",
    *,
    key_file: Path | None = None,
    known_hosts: Path | None = None,
    expected_fingerprint: str | None = None,
    sudo_password: str | None = None,
) -> paramiko.SSHClient:
    """Open a host-key-verified SSH session, retrying rate-limit drops."""
    if paramiko is None:
        raise SystemExit(
            "paramiko is required for deploy/vps/add_server.py. Install it first, "
            "for example: python -m pip install paramiko"
        )
    delay = 5.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if known_hosts is not None:
            resolved_known_hosts = known_hosts.expanduser()
            if resolved_known_hosts.exists():
                client.load_host_keys(str(resolved_known_hosts))
        if expected_fingerprint:
            client.set_missing_host_key_policy(
                PinnedHostKeyPolicy(expected_fingerprint)
            )
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=host,
                username=username,
                password=password,
                key_filename=str(key_file.expanduser()) if key_file else None,
                look_for_keys=key_file is None and password is None,
                allow_agent=key_file is None and password is None,
                timeout=60,
                banner_timeout=60,
                auth_timeout=60,
            )
            client._aegis_username = username  # type: ignore[attr-defined]
            client._aegis_sudo_password = sudo_password  # type: ignore[attr-defined]
            return client
        except HostKeyVerificationError as exc:
            client.close()
            raise SystemExit(str(exc)) from exc
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise SystemExit(
                f"SSH host key verification failed for {host}: {exc}"
            ) from exc
        except (paramiko.SSHException, EOFError, OSError) as exc:
            last = exc
            try:
                client.close()
            except Exception:
                pass
            if "known_hosts" in str(exc):
                raise SystemExit(
                    f"SSH host {host} is unknown. Add it to {known_hosts} only after verification "
                    "or pass its provider-supplied --*-host-fingerprint."
                ) from exc
            if attempt == attempts:
                break
            print(
                f"    ssh retry {attempt}/{attempts - 1} after {exc} (sleep {delay:.0f}s)"
            )
            time.sleep(delay)
            delay = min(delay * 1.7, 45.0)
    raise SystemExit(f"could not reach {host} after {attempts} attempts: {last}")


def exec_command(
    client: paramiko.SSHClient, command: str, timeout: int = 120
) -> tuple[int, str, str]:
    username = getattr(client, "_aegis_username", "root")
    stdin_data: str | None = None
    if username != "root":
        command = f"sudo -S -p '' -- sh -c {shell_quote(command)}"
        stdin_data = (getattr(client, "_aegis_sudo_password", None) or "") + "\n"
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if stdin_data is not None:
        stdin.write(stdin_data)
        stdin.flush()
        stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def run_or_die(
    client: paramiko.SSHClient, command: str, label: str, timeout: int = 120
) -> str:
    code, out, err = exec_command(client, command, timeout=timeout)
    if code != 0:
        raise SystemExit(
            f"{label} failed (exit {code}):\n{(err or out).strip()[-1000:]}"
        )
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
    if getattr(client, "_aegis_username", "root") != "root":
        temp_path = f"/tmp/.aegis-upload-{secrets.token_hex(12)}"
        sftp.put(str(local_path), temp_path)
        mode = 0o755 if local_path.stat().st_mode & stat.S_IXUSR else 0o600
        try:
            run_or_die(
                client,
                f"install -D -m {mode:o} {shell_quote(temp_path)} {shell_quote(remote_path)}",
                f"install {remote_path}",
            )
        finally:
            try:
                sftp.remove(temp_path)
            except OSError:
                pass
        return
    ensure_remote_dir(sftp, posixpath.dirname(remote_path))
    sftp.put(str(local_path), remote_path)
    if local_path.stat().st_mode & stat.S_IXUSR:
        sftp.chmod(remote_path, 0o755)


def write_remote_script(client: paramiko.SSHClient, path: str, content: str) -> None:
    sftp = get_sftp(client)
    if getattr(client, "_aegis_username", "root") != "root":
        temp_path = f"/tmp/.aegis-script-{secrets.token_hex(12)}"
        with sftp.file(temp_path, "w") as remote_file:
            remote_file.write(content)
        try:
            run_or_die(
                client,
                f"install -D -m 0755 {shell_quote(temp_path)} {shell_quote(path)}",
                f"install {path}",
            )
        finally:
            try:
                sftp.remove(temp_path)
            except OSError:
                pass
        return
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
set -eu
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "docker already installed: $(docker --version)"
    exit 0
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gpg >/dev/null
. /etc/os-release
SUITE=${UBUNTU_CODENAME:-$VERSION_CODENAME}
case "$ID:$SUITE" in
    debian:trixie) DIST_SUFFIX='debian.13~trixie' ;;
    ubuntu:noble) DIST_SUFFIX='ubuntu.24.04~noble' ;;
    ubuntu:resolute) DIST_SUFFIX='ubuntu.26.04~resolute' ;;
    *) echo "unsupported Docker package target: $ID $VERSION_ID ($SUITE)" >&2; exit 1 ;;
esac

install -m 0755 -d /etc/apt/keyrings
KEY_TMP=$(mktemp)
trap 'rm -f "$KEY_TMP"' EXIT
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$KEY_TMP"
FINGERPRINT=$(gpg --batch --show-keys --with-colons "$KEY_TMP" | awk -F: '$1 == "fpr" {print $10; exit}')
[ "$FINGERPRINT" = '9DC858229FC7DD38854AE2D88D81803C0EBFCD88' ] || {
    echo "unexpected Docker repository signing key: $FINGERPRINT" >&2; exit 1; }
install -m 0644 "$KEY_TMP" /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $SUITE
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    "docker-ce=5:29.7.1-1~$DIST_SUFFIX" \
    "docker-ce-cli=5:29.7.1-1~$DIST_SUFFIX" \
    "containerd.io=2.2.6-1~$DIST_SUFFIX" \
    "docker-buildx-plugin=0.36.0-1~$DIST_SUFFIX" \
    "docker-compose-plugin=5.3.1-1~$DIST_SUFFIX" >/dev/null
apt-mark hold docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
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
XRAY_PORT={args.xhttp_port}
XRAY_TCP_PORT={args.tcp_port}
XRAY_NETWORK={args.xray_network}
REALITY_DEST={args.reality_dest}
REALITY_SERVER_NAME={args.reality_server_name}
HOST_IP={args.server_domain}
{render_node_control_env(control_urls=args.control_url, mode="observe").rstrip()}
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


def pick_reality_keys(
    env: dict[str, str], xray_network: str = "xhttp"
) -> tuple[str, str]:
    """The node signs REALITY with ONE keypair, whatever the transport.

    entrypoint.sh builds every inbound — xhttp and tcp alike — from PRIVATE_KEY /
    SHORT_ID ("SINGLE shared reality keypair for every transport"). It still
    generates a *_TCP pair into agent.env, but nothing ever loads it.

    This used to hand the bot PUBLIC_KEY_TCP whenever tcp was the primary
    transport. That public key belongs to a private key the node never signs
    with, so every client of such a node failed its REALITY handshake. Always
    report the pair the node actually uses; the *_TCP values are inert.
    """
    pk = env.get("PUBLIC_KEY")
    sid = env.get("SHORT_ID")
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
    if (
        len(s) >= 2
        and 0x1F1E6 <= ord(s[0]) <= 0x1F1FF
        and 0x1F1E6 <= ord(s[1]) <= 0x1F1FF
    ):
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
    known_hosts = args.known_hosts.expanduser()
    main_password = None
    if args.main_key_file is None:
        main_password = getpass.getpass(
            f"SSH password for {args.main_username}@{args.main_host}: "
        )
    new_password = None
    if args.new_key_file is None:
        new_password = getpass.getpass(
            f"SSH password for {args.new_username}@{args.new_host}: "
        )
    main_sudo_password = None
    if args.main_username != "root":
        entered = getpass.getpass(
            f"sudo password for {args.main_username}@{args.main_host} "
            "(blank to reuse SSH password or for NOPASSWD): "
        )
        main_sudo_password = entered or main_password
    new_sudo_password = None
    if args.new_username != "root":
        entered = getpass.getpass(
            f"sudo password for {args.new_username}@{args.new_host} "
            "(blank to reuse SSH password or for NOPASSWD): "
        )
        new_sudo_password = entered or new_password
    try:
        main_control_ip = ip_address(args.main_host)
    except ValueError as exc:
        raise SystemExit(
            "--main-host must be the fixed IPv4 address allowed to reach "
            "the temporary observe-mode Agent API"
        ) from exc
    if not isinstance(main_control_ip, IPv4Address):
        raise SystemExit("--main-host must be a fixed IPv4 address")

    ca_cert, ca_key = ensure_control_ca(args.control_ca_dir.expanduser().resolve())
    credential_workspace = tempfile.TemporaryDirectory(prefix="aegis-node-control-")
    credentials = issue_node_credentials(
        ca_cert=ca_cert,
        ca_key=ca_key,
        output_dir=Path(credential_workspace.name),
        node_name=args.server_domain,
    )
    # XHTTP and TCP+VISION are separate inbounds — they cannot share a port.
    # (--tcp-port 0 disables the second inbound, so skip the check then.)
    if args.tcp_port != "0" and args.xhttp_port == args.tcp_port:
        raise SystemExit(
            f"--xhttp-port and --tcp-port must differ (both = {args.xhttp_port})"
        )
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
    new_client = connect(
        args.new_host,
        new_password,
        username=args.new_username,
        key_file=args.new_key_file,
        known_hosts=known_hosts,
        expected_fingerprint=args.new_host_fingerprint,
        sudo_password=new_sudo_password,
    )
    main_client: paramiko.SSHClient | None = None
    server_id: int | None = None
    try:
        print("[2/6] installing docker + tuning host network (BBR/fq/PMTU)…")
        write_remote_script(
            new_client, "/root/aegis/install_docker.sh", DOCKER_BOOTSTRAP
        )
        out = run_or_die(
            new_client,
            "sh /root/aegis/install_docker.sh",
            "docker install",
            timeout=600,
        )
        print("     " + out.strip().splitlines()[-1])
        write_remote_script(new_client, "/root/aegis/net_tuning.sh", NET_TUNING)
        # Best-effort: BBR may be unavailable on an exotic kernel; don't abort.
        code, tout, terr = exec_command(
            new_client, "sh /root/aegis/net_tuning.sh", timeout=60
        )
        print(
            "     "
            + ((tout or terr).strip().splitlines() or ["(net tuning skipped)"])[-1]
        )

        print("[3/6] uploading agent source + compose…")
        run_or_die(new_client, "mkdir -p /root/aegis/deploy/vps/data/vpn", "mkdir")
        upload_agent(new_client)
        upload_file(
            new_client, COMPOSE_FILE, "/root/aegis/deploy/vps/docker-compose.yml"
        )
        run_or_die(
            new_client,
            "install -d -m 0700 /root/aegis/deploy/vps/data/control/node",
            "create control credential directory",
        )
        for local_path in (
            credentials.client_cert,
            credentials.client_key,
            credentials.ca_cert,
            credentials.token_file,
        ):
            upload_file(
                new_client,
                local_path,
                f"/root/aegis/deploy/vps/data/control/node/{local_path.name}",
            )
        run_or_die(
            new_client,
            "chmod 0600 "
            "/root/aegis/deploy/vps/data/control/node/client.key "
            "/root/aegis/deploy/vps/data/control/node/token && "
            "chmod 0644 "
            "/root/aegis/deploy/vps/data/control/node/client.crt "
            "/root/aegis/deploy/vps/data/control/node/ca.crt",
            "protect control credentials",
        )

        print("[4/6] starting vpn container, waiting for Reality keys…")
        write_remote_script(new_client, REMOTE_SETUP_SCRIPT, build_setup_script(args))
        run_or_die(new_client, f"sh {REMOTE_SETUP_SCRIPT}", "vpn bring-up", timeout=600)
        write_remote_script(
            new_client,
            "/root/aegis/control-agent-firewall.sh",
            "#!/bin/sh\nset -eu\n"
            + render_agent_firewall(
                control_server_ip=str(main_control_ip),
                public_agent=True,
            ),
        )
        run_or_die(
            new_client,
            "sh /root/aegis/control-agent-firewall.sh",
            "restrict observe-mode Agent API",
        )

        out = run_or_die(
            new_client,
            "cat /root/aegis/deploy/vps/data/vpn/agent.env",
            "read agent.env",
        )
        remote_env = parse_env(out)
        public_key, short_id = pick_reality_keys(
            remote_env, xray_network=args.xray_network
        )
        agent_token = remote_env.get("AGENT_TOKEN")
        if not agent_token:
            raise SystemExit("agent.env missing AGENT_TOKEN")
        print(f"     keys ok (pbk={public_key[:8]}… sid={short_id})")

        if not args.no_warp:
            print("     registering a per-location WARP account…")
            write_remote_script(
                new_client, "/root/aegis/warp_register.sh", WARP_REGISTER_SCRIPT
            )
            # Best-effort: a WARP failure must not abort the deploy. The final
            # restart picks up agent.env's WARP_* via entrypoint ensure_warp().
            code, wout, werr = exec_command(
                new_client, "sh /root/aegis/warp_register.sh", timeout=120
            )
            tail = (wout or werr).strip().splitlines()[-1:] or ["(no output)"]
            print("     " + tail[0])

        print(f"[5/6] registering server on main VPS {args.main_host}…")
        main_client = connect(
            args.main_host,
            main_password,
            username=args.main_username,
            key_file=args.main_key_file,
            known_hosts=known_hosts,
            expected_fingerprint=args.main_host_fingerprint,
            sudo_password=main_sudo_password,
        )
        upload_file(main_client, MAIN_REGISTER_SCRIPT, REMOTE_REGISTER_SCRIPT)
        # All env values shell-quoted so emoji flags / spaces in names work
        # unchanged in the DB (no json.dumps escapes).
        register_command = (
            f"SERVER_NAME={shell_quote(server_name)} "
            f"SERVER_FLAG={shell_quote(server_flag)} "
            f"SERVER_HOST={shell_quote(args.server_domain)} "
            f"SERVER_PORT={shell_quote(args.xhttp_port)} "
            f"TCP_PORT={shell_quote(args.tcp_port)} "
            f"PUBLIC_KEY={shell_quote(public_key)} "
            f"SHORT_ID={shell_quote(short_id)} "
            f"AGENT_URL={shell_quote(agent_url)} "
            f"AGENT_TOKEN={shell_quote(agent_token)} "
            f"SUBSCRIPTION_GROUP={shell_quote(args.subscription_group)} "
            f"ACCESS_MODE={shell_quote(args.access_mode)} "
            f"COUNTRY_CODE={shell_quote(args.country_code or '')} "
            f"DISPLAY_ORDER={shell_quote(str(args.display_order))} "
            "CONTROL_MODE=observe "
            f"CONTROL_TOKEN_HASH={shell_quote(credentials.token_hash)} "
            "CONTROL_CERT_FINGERPRINT="
            f"{shell_quote(credentials.cert_fingerprint)} "
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
            print(
                "[warn] couldn't parse server_id from register output; "
                "resync + restart still needs to happen manually."
            )
            return 0

        print(
            f"[6/6] resync user UUIDs to server {server_id}; live Xray API "
            "keeps existing sessions intact…"
        )
        upload_file(main_client, MAIN_RESYNC_SCRIPT, REMOTE_RESYNC_SCRIPT)
        out = run_or_die(
            main_client,
            f"python3 {REMOTE_RESYNC_SCRIPT} {server_id}",
            "resync",
            timeout=300,
        )
        print("     " + out.strip())

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
                # `mtg run` takes a TOML CONFIG PATH, not a secret: passing the
                # secret positionally made mtg exit 80 immediately with
                # "stat /<secret>: no such file or directory" while every step
                # here still reported success, so the node advertised a proxy
                # link pointing at a dead port. `simple-run <bind> <secret>` is
                # the flag-free form (same as update.py's provision_mtproxy).
                # `compose run` also ignores the service's restart policy, so
                # the container is re-tagged unless-stopped explicitly.
                run_or_die(
                    new_client,
                    f"cd /root/aegis/deploy/vps && "
                    f"docker compose --profile mtproxy run -d --name aegis-mtg "
                    f"--no-deps mtg simple-run 0.0.0.0:80 {mtproxy_secret} 2>&1 | tail -2",
                    "start mtg",
                    timeout=60,
                )
                exec_command(
                    new_client,
                    "docker update --restart unless-stopped aegis-mtg",
                    timeout=30,
                )
                # Verify it is actually serving before claiming success — the
                # bug above stayed invisible precisely because nothing checked.
                mtg_code, mtg_state, _ = exec_command(
                    new_client,
                    "sleep 3; docker inspect -f '{{.State.Running}}' aegis-mtg",
                    timeout=30,
                )
                if mtg_code != 0 or mtg_state.strip() != "true":
                    _, mtg_log, _ = exec_command(
                        new_client, "docker logs aegis-mtg 2>&1 | tail -3", timeout=30
                    )
                    raise SystemExit(
                        f"MTProxy failed to start on {args.new_host}: {mtg_log.strip()}"
                    )
                # Store secret AND port in the bot DB. Server.mtproxy_capable
                # requires both — writing only the secret left mtproxy_port NULL,
                # so the node ran mtg happily and the bot never showed anyone the
                # link. (The success line below printed a working link anyway,
                # which is what made this look provisioned when it wasn't.)
                update_cmd = (
                    f"docker exec aegis-bot python3 -c "
                    f"'import asyncio; from sqlalchemy import text; from src.core.database import async_session_maker\n"
                    f'exec("""async def q():\\n'
                    f"    async with async_session_maker() as s:\\n"
                    f'        await s.execute(text(\\"UPDATE servers SET mtproxy_secret=\\\\\\"{mtproxy_secret}\\\\\\", mtproxy_port=80 WHERE id={server_id}\\"))\\n'
                    f"        await s.commit()\\n"
                    f'        print(\\"ok\\")\\n'
                    f'asyncio.run(q())""")\''
                )
                run_or_die(
                    main_client, update_cmd, "save mtproxy_secret + port", timeout=30
                )
                print(f"     MTProxy ready — secret={mtproxy_secret[:8]}…")
                print(
                    f"     Link: https://t.me/proxy?server={args.server_domain}&port=80&secret={mtproxy_secret}"
                )
            else:
                print("     [warn] failed to generate MTProxy secret, skipping.")
    finally:
        credential_workspace.cleanup()
        if main_client is not None:
            try:
                main_client.close()
            except Exception:
                pass
        try:
            new_client.close()
        except Exception:
            pass

    print("\n✓ Done.")
    print(f"   Name: {server_flag} {server_name}")
    print(
        f"   Host: {args.server_domain}:{args.xhttp_port}  (xhttp)  +  tcp+vision: {args.tcp_port}"
    )
    print(f"   Agent URL: {agent_url}")
    print(f"   Server ID in bot DB: {server_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
