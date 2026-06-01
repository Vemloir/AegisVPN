from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


PEERS_PATH = Path(os.environ.get("AMNEZIA_PEERS_PATH", "/etc/amneziawg/peers.json"))
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


def read_peers(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    payload = json.loads(raw)
    if not isinstance(payload, list):
        return []
    peers: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        public_key = str(item.get("public_key") or "").strip()
        ipv4 = str(item.get("ipv4") or "").strip()
        if public_key and ipv4:
            peers.append({"public_key": public_key, "ipv4": ipv4})
    return peers


def build_configs(settings: dict[str, str], peers: list[dict[str, str]]) -> tuple[str, str]:
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
        f"PostUp = iptables -C FORWARD -i {INTERFACE_NAME} -j ACCEPT 2>/dev/null || iptables -A FORWARD -i {INTERFACE_NAME} -j ACCEPT; "
        f"iptables -C FORWARD -o {INTERFACE_NAME} -j ACCEPT 2>/dev/null || iptables -A FORWARD -o {INTERFACE_NAME} -j ACCEPT; "
        f"iptables -t nat -C POSTROUTING -s {settings['SERVER_NETWORK']} -o {settings['UPLINK_IFACE']} -j MASQUERADE 2>/dev/null || "
        f"iptables -t nat -A POSTROUTING -s {settings['SERVER_NETWORK']} -o {settings['UPLINK_IFACE']} -j MASQUERADE",
        f"PostDown = iptables -D FORWARD -i {INTERFACE_NAME} -j ACCEPT 2>/dev/null || true; "
        f"iptables -D FORWARD -o {INTERFACE_NAME} -j ACCEPT 2>/dev/null || true; "
        f"iptables -t nat -D POSTROUTING -s {settings['SERVER_NETWORK']} -o {settings['UPLINK_IFACE']} -j MASQUERADE 2>/dev/null || true",
    ]

    for peer in peers:
        peer_lines = [
            "",
            "[Peer]",
            f"PublicKey = {peer['public_key']}",
            f"AllowedIPs = {peer['ipv4']}/32",
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
    peers = read_peers(PEERS_PATH)

    FULL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    full_config, runtime_config = build_configs(settings, peers)
    FULL_CONFIG_PATH.write_text(full_config, encoding="utf-8")
    RUNTIME_CONFIG_PATH.write_text(runtime_config, encoding="utf-8")

    if interface_exists():
        run(["awg", "syncconf", INTERFACE_NAME, str(RUNTIME_CONFIG_PATH)])
    else:
        run(["awg-quick", "up", str(FULL_CONFIG_PATH)])

    print(f"applied {len(peers)} peers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
