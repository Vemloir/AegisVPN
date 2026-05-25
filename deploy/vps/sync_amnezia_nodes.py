from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


DB_PATH = Path(os.environ.get("AMNEZIA_DB_PATH", "/root/aegis/deploy/vps/data/bot/aegis.db"))
LOCAL_APPLY_SCRIPT = Path(os.environ.get("AMNEZIA_LOCAL_APPLY_SCRIPT", "/root/aegis/deploy/vps/apply_amnezia_peers.py"))
SSH_KEY_PATH = Path(os.environ.get("AMNEZIA_SSH_KEY_PATH", "/root/.ssh/aegis_amnezia"))


def fetch_servers(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id, name, access_mode, agent_url, amnezia_endpoint_host, amnezia_port, amnezia_public_key
        FROM servers
        WHERE is_active = 1
          AND amnezia_enabled = 1
          AND amnezia_endpoint_host IS NOT NULL
          AND amnezia_public_key IS NOT NULL
          AND amnezia_port IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "access_mode": row[2],
            "agent_url": row[3],
            "endpoint_host": row[4],
            "port": row[5],
            "public_key": row[6],
        }
        for row in rows
    ]


def fetch_peers_for_server(conn: sqlite3.Connection, server_id: int, access_mode: str) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT ss.amnezia_public_key, ss.amnezia_ipv4
        FROM subscription_servers ss
        JOIN subscriptions s ON s.id = ss.subscription_id
        JOIN users u ON u.id = s.user_id
        LEFT JOIN server_access_grants g
          ON g.server_id = ? AND g.user_id = s.user_id
        WHERE ss.server_id = ?
          AND s.is_active = 1
          AND s.expires_at > datetime('now')
          AND ss.is_synced = 1
          AND u.is_banned = 0
          AND ss.amnezia_public_key IS NOT NULL
          AND ss.amnezia_ipv4 IS NOT NULL
          AND (? = 'public' OR g.user_id IS NOT NULL)
        ORDER BY ss.subscription_id
        """,
        (server_id, server_id, access_mode),
    ).fetchall()
    return [{"public_key": row[0], "ipv4": row[1]} for row in rows]


def run(cmd: list[str], *, input_text: str | None = None) -> None:
    subprocess.run(cmd, input=input_text, text=True, check=True)


def resolve_node_host(agent_url: str) -> str:
    host = urlsplit(agent_url).hostname
    if not host:
        raise RuntimeError(f"Cannot resolve node host from agent url: {agent_url}")
    return host


def sync_local(peers: list[dict[str, str]]) -> None:
    peers_path = Path("/etc/amneziawg/peers.json")
    peers_path.write_text(json.dumps(peers, ensure_ascii=False, indent=2), encoding="utf-8")
    run(["python3", str(LOCAL_APPLY_SCRIPT)])


def sync_remote(host: str, peers: list[dict[str, str]]) -> None:
    payload = json.dumps(peers, ensure_ascii=False, indent=2)
    remote_write = "cat > /etc/amneziawg/peers.json"
    run(
        [
            "ssh",
            "-i",
            str(SSH_KEY_PATH),
            "-o",
            "StrictHostKeyChecking=no",
            f"root@{host}",
            remote_write,
        ],
        input_text=payload,
    )
    run(
        [
            "ssh",
            "-i",
            str(SSH_KEY_PATH),
            "-o",
            "StrictHostKeyChecking=no",
            f"root@{host}",
            "python3 /root/aegis/deploy/vps/apply_amnezia_peers.py",
        ]
    )


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        servers = fetch_servers(conn)
        grouped: dict[str, dict[str, object]] = {}
        for server in servers:
            peers = fetch_peers_for_server(conn, int(server["id"]), str(server["access_mode"]))
            node_host = resolve_node_host(str(server["agent_url"]))
            entry = grouped.setdefault(
                node_host,
                {
                    "display_names": [],
                    "peers": {},
                },
            )
            entry["display_names"].append(str(server["name"]))
            peer_map: dict[str, dict[str, str]] = entry["peers"]  # type: ignore[assignment]
            for peer in peers:
                peer_map[peer["public_key"]] = peer

        for node_host, entry in grouped.items():
            peers = list(entry["peers"].values())  # type: ignore[index]
            display_names = ", ".join(entry["display_names"])  # type: ignore[index]
            if node_host in {"127.0.0.1", "localhost"}:
                sync_local(peers)
            else:
                sync_remote(node_host, peers)
            print(f"synced {len(peers)} peers to {display_names}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
