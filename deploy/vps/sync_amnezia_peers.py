from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path


DB_PATH = Path(os.environ.get("AMNEZIA_DB_PATH", "/root/aegis/deploy/vps/data/bot/aegis.db"))
ENV_PATH = Path(os.environ.get("AMNEZIA_ENV_PATH", "/etc/amneziawg/aegis_awg.env"))
FULL_CONFIG_PATH = Path(os.environ.get("AMNEZIA_CONFIG_PATH", "/etc/amneziawg/awg0.conf"))
RUNTIME_CONFIG_PATH = Path(os.environ.get("AMNEZIA_RUNTIME_CONFIG_PATH", "/etc/amneziawg/awg0.runtime.conf"))
INTERFACE_NAME = os.environ.get("AMNEZIA_INTERFACE", "awg0")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def fetch_active_peers(db_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT amnezia_public_key, amnezia_ipv4
            FROM subscriptions
            WHERE is_active = 1
              AND amnezia_public_key IS NOT NULL
              AND amnezia_ipv4 IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    return [(row[0], row[1]) for row in rows]


def build_configs(settings: dict[str, str], peers: list[tuple[str, str]]) -> tuple[str, str]:
    full_interface_lines = [
        "[Interface]",
        f"PrivateKey = {settings['SERVER_PRIVATE_KEY']}",
        f"Address = {settings['SERVER_ADDRESS']}",
        f"ListenPort = {settings['SERVER_PORT']}",
        f"Jc = {settings['Jc']}",
        f"Jmin = {settings['Jmin']}",
        f"Jmax = {settings['Jmax']}",
        f"S1 = {settings['S1']}",
        f"S2 = {settings['S2']}",
        f"H1 = {settings['H1']}",
        f"H2 = {settings['H2']}",
        f"H3 = {settings['H3']}",
        f"H4 = {settings['H4']}",
    ]
    runtime_lines = [
        "[Interface]",
        f"PrivateKey = {settings['SERVER_PRIVATE_KEY']}",
        f"ListenPort = {settings['SERVER_PORT']}",
        f"Jc = {settings['Jc']}",
        f"Jmin = {settings['Jmin']}",
        f"Jmax = {settings['Jmax']}",
        f"S1 = {settings['S1']}",
        f"S2 = {settings['S2']}",
        f"H1 = {settings['H1']}",
        f"H2 = {settings['H2']}",
        f"H3 = {settings['H3']}",
        f"H4 = {settings['H4']}",
    ]

    full_lines = full_interface_lines + [
        f"PostUp = iptables -A FORWARD -i {INTERFACE_NAME} -j ACCEPT; iptables -A FORWARD -o {INTERFACE_NAME} -j ACCEPT; iptables -t nat -A POSTROUTING -s {settings['SERVER_NETWORK']} -o {settings['UPLINK_IFACE']} -j MASQUERADE",
        f"PostDown = iptables -D FORWARD -i {INTERFACE_NAME} -j ACCEPT; iptables -D FORWARD -o {INTERFACE_NAME} -j ACCEPT; iptables -t nat -D POSTROUTING -s {settings['SERVER_NETWORK']} -o {settings['UPLINK_IFACE']} -j MASQUERADE",
    ]

    for public_key, client_ipv4 in peers:
        peer_lines = [
            "",
            "[Peer]",
            f"PublicKey = {public_key}",
            f"AllowedIPs = {client_ipv4}/32",
        ]
        full_lines.extend(peer_lines)
        runtime_lines.extend(peer_lines)

    return "\n".join(full_lines).strip() + "\n", "\n".join(runtime_lines).strip() + "\n"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def interface_exists() -> bool:
    result = subprocess.run(["ip", "link", "show", INTERFACE_NAME], check=False, capture_output=True, text=True)
    return result.returncode == 0


def main() -> int:
    settings = read_env_file(ENV_PATH)
    required = {
        "SERVER_PRIVATE_KEY",
        "SERVER_ADDRESS",
        "SERVER_NETWORK",
        "SERVER_PORT",
        "UPLINK_IFACE",
        "Jc",
        "Jmin",
        "Jmax",
        "S1",
        "S2",
        "H1",
        "H2",
        "H3",
        "H4",
    }
    missing = [key for key in required if not settings.get(key)]
    if missing:
        raise SystemExit(f"Missing settings in {ENV_PATH}: {', '.join(missing)}")

    peers = fetch_active_peers(DB_PATH)
    FULL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    full_config, runtime_config = build_configs(settings, peers)
    FULL_CONFIG_PATH.write_text(full_config, encoding="utf-8")
    RUNTIME_CONFIG_PATH.write_text(runtime_config, encoding="utf-8")

    if interface_exists():
        run(["awg", "syncconf", INTERFACE_NAME, str(RUNTIME_CONFIG_PATH)])
    else:
        run(["awg-quick", "up", str(FULL_CONFIG_PATH)])

    print(f"synced {len(peers)} peers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
