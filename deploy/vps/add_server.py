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
        "paramiko is required for deploy/vps/add_server.py. Install it first, for example: "
        "python -m pip install paramiko"
    ) from exc


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
COMPOSE_FILE = ROOT / "deploy" / "vps" / "docker-compose.yml"
MAIN_REGISTER_SCRIPT = ROOT / ".codex_tmp" / "register_external_server.py"
REMOTE_SETUP_SCRIPT = "/root/aegis/setup_server.sh"
REMOTE_REGISTER_SCRIPT = "/root/aegis/register_external_server.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision and register a new Aegis VPN server.")
    parser.add_argument("--main-host", required=True)
    parser.add_argument("--main-password", required=True)
    parser.add_argument("--new-host", required=True)
    parser.add_argument("--new-password", required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--server-flag", required=True)
    parser.add_argument("--server-domain", required=True)
    parser.add_argument("--agent-url", help="Defaults to http://<new-host>:8444")
    parser.add_argument("--xray-port", default="443")
    parser.add_argument("--reality-dest", default="gateway.icloud.com:443")
    parser.add_argument("--reality-server-name", default="gateway.icloud.com")
    return parser.parse_args()


def connect(host: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username="root", password=password, timeout=30)
    return client


def exec_command(client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
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


def upload_file(client: paramiko.SSHClient, local_path: Path, remote_path: str) -> None:
    sftp = client.open_sftp()
    try:
        ensure_remote_dir(sftp, posixpath.dirname(remote_path))
        sftp.put(str(local_path), remote_path)
        mode = local_path.stat().st_mode
        if mode & stat.S_IXUSR:
            sftp.chmod(remote_path, 0o755)
    finally:
        sftp.close()


def upload_agent(client: paramiko.SSHClient) -> None:
    files = [
        AGENT_DIR / "Dockerfile",
        AGENT_DIR / "entrypoint.sh",
        AGENT_DIR / "pyproject.toml",
        AGENT_DIR / "uv.lock",
        AGENT_DIR / "template.json",
        AGENT_DIR / "app" / "main.py",
        AGENT_DIR / "app" / "config.py",
        AGENT_DIR / "app" / "models.py",
    ]
    for file in files:
        remote = "/root/aegis/agent/" + file.relative_to(AGENT_DIR).as_posix()
        upload_file(client, file, remote)


def build_setup_script(args: argparse.Namespace) -> str:
    return f"""#!/bin/sh
set -eu
mkdir -p /root/aegis /root/aegis/deploy/vps /root/aegis/deploy/vps/data/vpn
cat > /root/aegis/deploy/vps/vpn.env <<'EOF'
XRAY_RUN_MODE=internal
XRAY_CONFIG_PATH=/data/xray-config.json
XRAY_PORT={args.xray_port}
XRAY_TCP_PORT=0
REALITY_DEST={args.reality_dest}
REALITY_SERVER_NAME={args.reality_server_name}
HOST_IP={args.server_domain}
EOF
cd /root/aegis/deploy/vps
docker compose up -d --build vpn
"""


def write_remote_script(client: paramiko.SSHClient, path: str, content: str) -> None:
    sftp = client.open_sftp()
    try:
        ensure_remote_dir(sftp, posixpath.dirname(path))
        with sftp.file(path, "w") as remote_file:
            remote_file.write(content)
        sftp.chmod(path, 0o755)
    finally:
        sftp.close()


def parse_env(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def main() -> int:
    args = parse_args()
    agent_url = args.agent_url or f"http://{args.new_host}:8444"

    if not MAIN_REGISTER_SCRIPT.exists():
        raise SystemExit(f"Missing helper script: {MAIN_REGISTER_SCRIPT}")

    new_client = connect(args.new_host, args.new_password)
    try:
        code, _, err = exec_command(new_client, "mkdir -p /root/aegis /root/aegis/deploy/vps /root/aegis/deploy/vps/data/vpn")
        if code != 0:
            raise SystemExit(err or "Failed to prepare remote directories")
        upload_agent(new_client)
        upload_file(new_client, COMPOSE_FILE, "/root/aegis/deploy/vps/docker-compose.yml")
        write_remote_script(new_client, REMOTE_SETUP_SCRIPT, build_setup_script(args))
        code, out, err = exec_command(new_client, f"sh {REMOTE_SETUP_SCRIPT}")
        if code != 0:
            raise SystemExit(err or out or "Failed to start remote VPN")
        time.sleep(3)
        code, env_out, err = exec_command(new_client, "cat /root/aegis/deploy/vps/data/vpn/agent.env")
        if code != 0:
            raise SystemExit(err or "Failed to read remote agent.env")
        remote_env = parse_env(env_out)
    finally:
        new_client.close()

    main_client = connect(args.main_host, args.main_password)
    try:
        upload_file(main_client, MAIN_REGISTER_SCRIPT, REMOTE_REGISTER_SCRIPT)
        register_command = (
            f"SERVER_NAME={json.dumps(args.server_name)} "
            f"SERVER_FLAG={json.dumps(args.server_flag)} "
            f"SERVER_HOST={json.dumps(args.server_domain)} "
            f"SERVER_PORT={json.dumps(args.xray_port)} "
            f"PUBLIC_KEY={json.dumps(remote_env['PUBLIC_KEY'])} "
            f"SHORT_ID={json.dumps(remote_env['SHORT_ID'])} "
            f"AGENT_URL={json.dumps(agent_url)} "
            f"AGENT_TOKEN={json.dumps(remote_env['AGENT_TOKEN'])} "
            f"python3 {REMOTE_REGISTER_SCRIPT}"
        )
        code, out, err = exec_command(main_client, register_command)
        if code != 0:
            raise SystemExit(err or out or "Failed to register server on main VPS")
        print(out.strip())
    finally:
        main_client.close()

    print("Server is provisioned and registered.")
    print(f"Name: {args.server_flag} {args.server_name}")
    print(f"Host: {args.server_domain}:{args.xray_port}")
    print(f"Agent URL: {agent_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
