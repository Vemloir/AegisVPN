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
    """Upload all bot Python sources and recreate the bot container only."""
    print("  uploading bot sources…")
    for py in sorted((BOT_DIR / "src").rglob("*.py")):
        rel = py.relative_to(BOT_DIR)
        upload(c, py, f"/root/aegis/bot/{rel.as_posix()}")

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

def update_agent(c: paramiko.SSHClient, host: str) -> None:
    """Upload all agent sources and restart only the vpn container."""
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

    print(f"  [{host}] rebuilding + restarting vpn…")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --build --no-deps vpn 2>&1 | tail -4",
        "vpn rebuild", timeout=300)
    print(f"  [{host}] agent updated ✓")


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
        "--mtproxy", action="store_true",
        help="Set up MTProxy (Telegram proxy, port 80) on --node targets. "
             "Requires --main-password and --node SERVER_ID:IP:PASSWORD.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.bot and not args.nodes and not args.mtproxy:
        raise SystemExit("Specify --bot, --nodes, and/or --mtproxy")
    if args.bot and not args.main_password:
        raise SystemExit("--bot requires --main-password")
    if (args.nodes or args.mtproxy) and not args.nodes_list:
        raise SystemExit("--nodes/--mtproxy requires at least one --node")
    if args.mtproxy and not args.main_password:
        raise SystemExit("--mtproxy requires --main-password (to update the bot DB)")

    if args.bot:
        print(f"[bot] connecting to {args.main_host}…")
        c = connect(args.main_host, args.main_password)
        try:
            update_bot(c)
        finally:
            c.close()

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
