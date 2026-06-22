"""Push code updates to running AegisVPN nodes.

Two independent targets:
  --bot   : upload changed bot sources to the main VPS and recreate the bot
            container WITHOUT touching aegis-vpn (avoids the depends_on restart).
  --nodes : upload updated agent sources to one or more VPN nodes and restart
            only the vpn container there.

Both targets can be combined in one run.

Usage examples:

  # Update the bot only (main VPS):
  python update.py --main-host MAIN_IP --main-password '…' --bot

  # Update agent on two nodes:
  python update.py --node NODE_IP:ROOT_PASSWORD \\
                   --node NODE_IP:ROOT_PASSWORD \\
                   --nodes

  # Both at once:
  python update.py --main-host MAIN_IP --main-password '…' --bot \\
                   --node NODE_IP:ROOT_PASSWORD --nodes

  # Switch nodes' Xray transport (xhttp|tcp) and mirror reality keys into the
  # bot DB in one atomic pass (needs --main-password for the DB write):
  python update.py --set-network xhttp --main-host MAIN_IP --main-password '…' \\
                   --node NODE_IP:ROOT_PASSWORD --node NODE_IP:ROOT_PASSWORD

  # Roll the xhttp tuning (server mode=auto, connIdle=60, tcpKeepAlive) onto
  # already-xhttp nodes WITHOUT breaking the live subscriptions: push the new
  # agent (brings the connIdle/sockopt entrypoint logic) AND re-sync the env via
  # --set-network xhttp (rewrites XHTTP_MODE=auto). auto keeps existing packet-up
  # clients working — no re-import. Combine both in one invocation:
  python update.py --nodes --set-network xhttp \\
                   --main-host MAIN_IP --main-password '…' \\
                   --node NODE_IP:ROOT_PASSWORD --node NODE_IP:ROOT_PASSWORD

  # One-time: migrate nodes to the split xray+agent topology so future code
  # deploys (--nodes) recreate ONLY the agent and drop no client sessions:
  python update.py --split-migrate \\
                   --node NODE_IP:ROOT_PASSWORD --node NODE_IP:ROOT_PASSWORD
"""

from __future__ import annotations

import argparse
import posixpath
import stat
import time
from pathlib import Path

try:
    import paramiko
except ImportError as exc:
    raise SystemExit("paramiko required: pip install paramiko") from exc

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
BOT_DIR = ROOT / "bot"


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def connect(host: str, password: str, attempts: int = 4) -> paramiko.SSHClient:
    delay = 8.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(host, username="root", password=password,
                      timeout=30, banner_timeout=30, auth_timeout=30)
            return c
        except Exception as exc:
            last = exc
            try: c.close()
            except Exception: pass
            if attempt == attempts:
                break
            print(f"  ssh retry {attempt}/{attempts-1} ({exc}) sleep {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 1.7, 45.0)
    raise SystemExit(f"cannot reach {host}: {last}")


def run(c: paramiko.SSHClient, cmd: str, label: str = "", timeout: int = 120) -> str:
    _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise SystemExit(f"{label or cmd} failed (exit {rc}):\n{(err or out).strip()[-800:]}")
    return out


def get_sftp(c: paramiko.SSHClient) -> paramiko.SFTPClient:
    sftp = getattr(c, "_sftp", None)
    if sftp is None:
        sftp = c.open_sftp()
        c._sftp = sftp  # type: ignore[attr-defined]
    return sftp


def upload(c: paramiko.SSHClient, local: Path, remote: str) -> None:
    sftp = get_sftp(c)
    remote_dir = posixpath.dirname(remote)
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        run(c, f"mkdir -p {remote_dir}")
    sftp.put(str(local), remote)
    if local.stat().st_mode & stat.S_IXUSR:
        sftp.chmod(remote, 0o755)


# ---------------------------------------------------------------------------
# Bot update
# ---------------------------------------------------------------------------

def update_bot(c: paramiko.SSHClient) -> None:
    """Upload all bot sources (code + assets) and recreate the bot container only."""
    print("  uploading bot sources…")
    # Upload EVERY source asset, not just .py — the privacy/ToS markdown (and any
    # future templates/json) live under src/ too and must reach the image. A
    # .py-only sweep silently shipped stale .md files and dropped new assets.
    for f in sorted((BOT_DIR / "src").rglob("*")):
        if f.is_dir() or "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        rel = f.relative_to(BOT_DIR)
        upload(c, f, f"/root/aegis/bot/{rel.as_posix()}")

    # Dependency manifests must ship too — the image installs from pyproject.toml,
    # so a dependency change is invisible to the build unless these are uploaded.
    for manifest in ("pyproject.toml", "uv.lock"):
        src = BOT_DIR / manifest
        if src.exists():
            upload(c, src, f"/root/aegis/bot/{manifest}")

    # Recreate bot container WITHOUT going through docker compose up
    # (which would also restart aegis-vpn via depends_on).
    print("  rebuilding image…")
    run(c, "cd /root/aegis/deploy/vps && docker compose build bot 2>&1 | tail -3",
        "docker build bot", timeout=180)

    print("  recreating container (vpn untouched)…")
    run(c, "docker stop aegis-bot 2>/dev/null || true && docker rm aegis-bot 2>/dev/null || true",
        "remove old bot")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --no-deps bot 2>&1",
        "start bot", timeout=60)
    print("  bot updated ✓")


# ---------------------------------------------------------------------------
# Agent (node) update
# ---------------------------------------------------------------------------

COMPOSE_LOCAL = ROOT / "deploy/vps/docker-compose.yml"
REMOTE_COMPOSE = "/root/aegis/deploy/vps/docker-compose.yml"


def _upload_agent_sources(c: paramiko.SSHClient, host: str) -> None:
    print(f"  [{host}] uploading agent sources…")
    base_files = [
        AGENT_DIR / "Dockerfile",
        AGENT_DIR / "entrypoint.sh",
        AGENT_DIR / "pyproject.toml",
        AGENT_DIR / "uv.lock",
        AGENT_DIR / "template.json",
    ]
    for f in base_files + sorted((AGENT_DIR / "app").glob("*.py")):
        rel = f.relative_to(AGENT_DIR)
        upload(c, f, f"/root/aegis/agent/{rel.as_posix()}")


def update_agent(c: paramiko.SSHClient, host: str) -> None:
    """Upload agent sources and recreate ONLY the agent container.

    In the split topology xray runs in its own container, so rebuilding `agent`
    leaves the data plane (and every live client session) untouched — code
    deploys are zero-drop. Run --split-migrate once first to create the split.
    """
    _upload_agent_sources(c, host)
    print(f"  [{host}] rebuilding + recreating agent (xray untouched)…")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --build --no-deps agent 2>&1 | tail -4",
        "agent rebuild", timeout=300)
    print(f"  [{host}] agent updated ✓")


def split_migrate(c: paramiko.SSHClient, host: str) -> None:
    """One-time: migrate a node from the single `vpn` container to the split
    `xray` + `agent` topology. Costs one brief xray blip at cutover; afterwards
    `--nodes` recreates only `agent`, so future code deploys drop no sessions.
    """
    _upload_agent_sources(c, host)
    print(f"  [{host}] uploading docker-compose.yml…")
    upload(c, COMPOSE_LOCAL, REMOTE_COMPOSE)
    print(f"  [{host}] building split images (old container still serving)…")
    run(c, "cd /root/aegis/deploy/vps && docker compose build xray agent 2>&1 | tail -4",
        "split build", timeout=400)
    print(f"  [{host}] cutover: dropping single container, bringing up xray+agent…")
    # The old `vpn` service holds the aegis-vpn name the new `agent` service
    # wants; remove it (also stops the bundled xray) then start the split. xray
    # reads the existing config straight off the shared ./data/vpn volume.
    run(c, "docker rm -f aegis-vpn 2>/dev/null || true", "rm old vpn")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --no-deps xray agent 2>&1 | tail -6",
        "split up", timeout=120)
    out = run(c, "sleep 5; docker ps --filter name=aegis-xray --filter name=aegis-vpn "
                 "--format '{{.Names}} {{.Status}}'", "verify split", timeout=60)
    print(f"  [{host}] live containers:\n    " + "\n    ".join(out.strip().splitlines() or ["(none!)"]))


# ---------------------------------------------------------------------------
# Transport switch (xhttp <-> tcp)
# ---------------------------------------------------------------------------

# agent.env lives on the host via the vpn service's ./data/vpn:/data volume.
REMOTE_AGENT_ENV = "/root/aegis/deploy/vps/data/vpn/agent.env"


def _parse_env(blob: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _render_env(env: dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in env.items())


def set_network(
    c: paramiko.SSHClient, host: str, network: str, xhttp_mode: str = "auto"
) -> tuple[str, str]:
    """Flip XRAY_NETWORK in the node's agent.env and restart the vpn container.

    Returns (public_key, short_id) matching the *target* transport. Reality uses
    a different keypair per transport, and the bot overrides pbk/sid from its DB
    in normalize_vless_uri — so these MUST be mirrored into the DB or the reality
    handshake breaks. Clients are preserved: entrypoint rebuilds the inbound from
    the same on-disk config, only adding/stripping the vision flow as needed.

    For xhttp, the server mode is forced to `xhttp_mode` (default "auto"): auto
    accepts existing packet-up clients AND new stream-up/stream-one ones, so the
    15 live subscriptions keep working with no re-import. An explicit non-auto
    mode would 400 every mismatched client — don't pass one unless re-issuing.
    """
    sftp = get_sftp(c)
    with sftp.open(REMOTE_AGENT_ENV, "r") as fh:
        env = _parse_env(fh.read().decode())

    current = (env.get("XRAY_NETWORK") or "tcp").lower()
    if network == "xhttp":
        pubkey, sid = env.get("PUBLIC_KEY"), env.get("SHORT_ID")
        env.setdefault("XHTTP_PATH", "/")
        env["XHTTP_MODE"] = xhttp_mode
        env.setdefault("XRAY_CONN_IDLE", "60")
    else:
        pubkey = env.get("PUBLIC_KEY_TCP") or env.get("PUBLIC_KEY")
        sid = env.get("SHORT_ID_TCP") or env.get("SHORT_ID")
    if not pubkey or not sid:
        raise SystemExit(
            f"[{host}] agent.env missing reality keys for {network} "
            f"(pubkey={bool(pubkey)}, sid={bool(sid)}) — aborting"
        )

    if current == network:
        print(f"  [{host}] XRAY_NETWORK already '{network}' — re-syncing keys anyway")
    env["XRAY_NETWORK"] = network
    print(f"  [{host}] XRAY_NETWORK {current} -> {network}")
    if network == "xhttp":
        print(f"  [{host}] XHTTP_MODE -> {xhttp_mode}, connIdle -> {env['XRAY_CONN_IDLE']}s")
    with sftp.open(REMOTE_AGENT_ENV, "w") as fh:
        fh.write(_render_env(env))

    # Split topology only (run --split-migrate first): recreate the agent so it
    # rebuilds the config from the new env, then restart xray to load it. A
    # transport change can't be hot-applied, so this xray bounce drops sessions
    # once — unavoidable for a mode/keypair change.
    print(f"  [{host}] applying: recreate agent + reload xray…")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --no-deps --force-recreate agent 2>&1 | tail -3",
        "agent recreate", timeout=180)
    run(c, "cd /root/aegis/deploy/vps && docker compose restart xray 2>&1 | tail -2",
        "xray reload", timeout=120)
    out = run(
        c,
        "sleep 5; docker exec aegis-vpn sh -c "
        "'grep -m1 -o \"\\\"network\\\": \\\"[a-z]*\\\"\" "
        "\"${XRAY_CONFIG_PATH:-/etc/xray/config.json}\"' || true",
        "verify network", timeout=60,
    )
    print(f"  [{host}] live inbound: {out.strip() or '(unverified)'}")
    return pubkey, sid


def set_db_keys(main_c: paramiko.SSHClient, host: str, pubkey: str, sid: str) -> None:
    """Mirror a node's active reality pubkey/short_id into its bot DB row."""
    py = (
        "import asyncio\n"
        "from sqlalchemy import text\n"
        "from src.core.database import async_session_maker\n"
        f"PK, SID, HOST = {pubkey!r}, {sid!r}, {host!r}\n"
        "async def q():\n"
        "    async with async_session_maker() as s:\n"
        "        res = await s.execute(text('UPDATE servers SET public_key=:pk, short_id=:sid WHERE host=:h'),"
        " {'pk': PK, 'sid': SID, 'h': HOST})\n"
        "        await s.commit()\n"
        "        print('rows updated:', res.rowcount)\n"
        "        r = await s.execute(text('SELECT id, name, host, public_key, short_id FROM servers WHERE host=:h'),"
        " {'h': HOST})\n"
        "        for row in r.fetchall(): print('  now:', tuple(row))\n"
        "asyncio.run(q())\n"
    )
    cmd = "docker exec -i aegis-bot python3 <<'PYEOF'\n" + py + "PYEOF\n"
    out = run(main_c, cmd, f"db update {host}", timeout=60)
    print(out.rstrip())
    if "rows updated: 0" in out:
        raise SystemExit(f"[{host}] no servers row matched host={host} — DB NOT updated")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def setup_mtproxy(c: paramiko.SSHClient, host: str, main_c: paramiko.SSHClient, server_id: int) -> str | None:
    """Generate an MTProxy secret, start mtg on port 80, store secret in DB. Returns secret or None."""
    print(f"  [{host}] generating MTProxy secret…")
    _, out, _ = c.exec_command("docker run --rm ghcr.io/9seconds/mtg:2 generate-secret google.com 2>/dev/null", timeout=60)
    rc = out.channel.recv_exit_status()
    secret = out.read().decode().strip()
    if rc != 0 or not secret:
        print(f"  [{host}] failed to generate secret, skipping MTProxy")
        return None

    print(f"  [{host}] starting mtg on port 80…")
    run(c,
        f"docker rm -f aegis-mtg 2>/dev/null || true && "
        f"docker run -d --name aegis-mtg --network host --restart unless-stopped "
        f"ghcr.io/9seconds/mtg:2 run {secret} --bind 0.0.0.0:80 2>&1 | tail -1",
        "start mtg", timeout=60)

    # Save secret to bot DB
    update_cmd = (
        f"docker exec aegis-bot python3 -c "
        f"'import asyncio; from sqlalchemy import text; from src.core.database import async_session_maker\n"
        f"exec(\"\"\"async def q():\\n"
        f"    async with async_session_maker() as s:\\n"
        f"        await s.execute(text(\\\"UPDATE servers SET mtproxy_secret=\\\\\\\"{secret}\\\\\\\" WHERE id={server_id}\\\"))\\n"
        f"        await s.commit()\\n"
        f"asyncio.run(q())\"\"\")'"
    )
    run(main_c, update_cmd, "save secret", timeout=30)
    print(f"  [{host}] MTProxy ready ✓  secret={secret[:8]}…")
    return secret


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Push code updates to AegisVPN nodes.")
    p.add_argument("--main-host", default="89.125.181.236")
    p.add_argument("--main-password", required=False)
    p.add_argument("--bot", action="store_true", help="Update the bot on the main VPS")
    p.add_argument("--nodes", action="store_true", help="Update agent on --node targets")
    p.add_argument(
        "--node", action="append", dest="nodes_list", metavar="IP:PASSWORD",
        help="Node to update (repeatable). Format: ip:password",
    )
    p.add_argument(
        "--set-network", choices=["xhttp", "tcp"], default=None,
        help="Switch the Xray transport on --node targets and mirror the matching "
             "reality keys into the bot DB. Requires --main-password.",
    )
    p.add_argument(
        "--xhttp-mode", choices=["auto", "stream-one", "stream-up", "packet-up"],
        default="auto",
        help="Server xhttp mode when --set-network xhttp (default: auto). auto is "
             "the only non-breaking value — it accepts existing packet-up clients "
             "plus new stream-up/stream-one ones. An explicit mode 400s mismatched "
             "clients, so only use it when re-issuing every subscription.",
    )
    p.add_argument(
        "--mtproxy", action="store_true",
        help="Set up MTProxy (Telegram proxy, port 80) on --node targets. "
             "Requires --main-password and --node SERVER_ID:IP:PASSWORD.",
    )
    p.add_argument(
        "--split-migrate", action="store_true",
        help="One-time: migrate --node targets from the single vpn container to "
             "the split xray+agent topology (zero-drop code deploys afterwards). "
             "Uploads docker-compose.yml + agent sources. One brief xray blip.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not any((args.bot, args.nodes, args.mtproxy, args.set_network, args.split_migrate)):
        raise SystemExit("Specify --bot, --nodes, --mtproxy, --set-network, and/or --split-migrate")
    if args.bot and not args.main_password:
        raise SystemExit("--bot requires --main-password")
    if (args.nodes or args.mtproxy) and not args.nodes_list:
        raise SystemExit("--nodes/--mtproxy requires at least one --node")
    if args.mtproxy and not args.main_password:
        raise SystemExit("--mtproxy requires --main-password (to update the bot DB)")
    if args.split_migrate and not args.nodes_list:
        raise SystemExit("--split-migrate requires at least one --node IP:PASSWORD")
    if args.set_network and not args.nodes_list:
        raise SystemExit("--set-network requires at least one --node IP:PASSWORD")
    if args.set_network and not args.main_password:
        raise SystemExit("--set-network requires --main-password (to update the bot DB)")

    if args.bot:
        print(f"[bot] connecting to {args.main_host}…")
        c = connect(args.main_host, args.main_password)
        try:
            update_bot(c)
        finally:
            c.close()

    if args.split_migrate:
        for node_str in args.nodes_list:
            parts = node_str.split(":")
            if len(parts) < 2:
                raise SystemExit(f"Bad --node format (expected IP:PASSWORD): {node_str}")
            ip, password = parts[0], ":".join(parts[1:])
            print(f"[split-migrate {ip}] connecting…")
            c = connect(ip, password)
            try:
                split_migrate(c, ip)
            finally:
                c.close()

    if args.set_network:
        results: list[tuple[str, str, str]] = []  # (ip, pubkey, sid)
        for node_str in args.nodes_list:
            parts = node_str.split(":")
            if len(parts) < 2:
                raise SystemExit(f"Bad --node format (expected IP:PASSWORD): {node_str}")
            ip, password = parts[0], ":".join(parts[1:])
            print(f"[set-network {ip}] connecting…")
            c = connect(ip, password)
            try:
                pubkey, sid = set_network(c, ip, args.set_network, args.xhttp_mode)
                results.append((ip, pubkey, sid))
            finally:
                c.close()

        print(f"[db] connecting to {args.main_host}…")
        mc = connect(args.main_host, args.main_password)
        try:
            for ip, pubkey, sid in results:
                set_db_keys(mc, ip, pubkey, sid)
        finally:
            mc.close()

    main_c: paramiko.SSHClient | None = None

    if args.nodes or args.mtproxy:
        for node_str in args.nodes_list:
            parts = node_str.split(":")
            if args.mtproxy:
                # Format: SERVER_ID:IP:PASSWORD
                if len(parts) < 3:
                    raise SystemExit(f"--mtproxy expects SERVER_ID:IP:PASSWORD, got: {node_str}")
                server_id, ip, password = int(parts[0]), parts[1], ":".join(parts[2:])
            else:
                # Format: IP:PASSWORD
                if len(parts) < 2:
                    raise SystemExit(f"Bad --node format (expected IP:PASSWORD): {node_str}")
                ip, password = parts[0], ":".join(parts[1:])
                server_id = 0

            print(f"[node {ip}] connecting…")
            c = connect(ip, password)
            try:
                if args.nodes:
                    update_agent(c, ip)
                if args.mtproxy:
                    if main_c is None:
                        main_c = connect(args.main_host, args.main_password)
                    setup_mtproxy(c, ip, main_c, server_id)
            finally:
                c.close()

    if main_c is not None:
        main_c.close()

    print("\nAll done.")


if __name__ == "__main__":
    main()
