"""Register a freshly provisioned node in the bot's database and sync it.

Runs ON THE MAIN HOST (where the bot and its SQLite file live), uploaded and
invoked by add_server.py. It writes the `servers` row, then pushes every active
subscription — and every one of its device UUIDs — to the new node's agent.

This used to live in an untracked scratch directory, which meant a fresh clone
of this repository could not add a server at all: add_server.py referenced a file
that simply wasn't there. It is part of the deploy surface, so it lives here.

Every field the bot reads about a node is settable here, because anything left to
its default is a way for a new node to quietly differ from the existing ones.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("BOT_DB_PATH", "/root/aegis/deploy/vps/data/bot/aegis.db"))


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing env: {name}")
    return value


def env_optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def add_client(agent_url: str, agent_token: str, client_uuid: str, email: str) -> bool:
    payload = json.dumps({"uuid": client_uuid, "email": email}).encode("utf-8")
    request = urllib.request.Request(
        f"{agent_url.rstrip('/')}/client/add",
        data=payload,
        headers={
            "Authorization": f"Bearer {agent_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
            return bool(data.get("added") or data.get("status") == "ok")
    except urllib.error.URLError as exc:
        print(f"Failed to sync {client_uuid}: {exc}")
        return False


def main() -> None:
    server_name = env("SERVER_NAME")
    server_flag = env("SERVER_FLAG")
    server_host = env("SERVER_HOST")
    server_port = int(env("SERVER_PORT"))
    public_key = env("PUBLIC_KEY")
    short_id = env("SHORT_ID")
    agent_url = env("AGENT_URL")
    agent_token = env("AGENT_TOKEN")
    subscription_group = env_optional("SUBSCRIPTION_GROUP", "safe") or "safe"
    access_mode = env_optional("ACCESS_MODE", "public") or "public"
    is_active = 0 if env_optional("IS_ACTIVE", "1") == "0" else 1
    # ISO 3166-1 alpha-2. The website's globe keys its country outline off this;
    # a node without it is served by the bot but never drawn on the site.
    country_code = (env_optional("COUNTRY_CODE") or "").upper() or None
    _order_raw = env_optional("DISPLAY_ORDER", "0")
    display_order = int(_order_raw) if _order_raw.lstrip("-").isdigit() else 0
    # Alt TCP+VISION inbound port on the same reality keypair. A positive value
    # makes the bot offer the transport choice (Server.has_alt_transports /
    # available_transports key on tcp_port); 0/empty -> NULL (xhttp-only node).
    _tcp_raw = env_optional("TCP_PORT", "0")
    tcp_port = int(_tcp_raw) if _tcp_raw.lstrip("-").isdigit() and int(_tcp_raw) > 0 else None
    control_mode = env_optional("CONTROL_MODE", "push") or "push"
    if control_mode not in {"push", "observe", "pull"}:
        raise SystemExit(f"Invalid CONTROL_MODE: {control_mode}")
    control_token_hash = env_optional("CONTROL_TOKEN_HASH") or None
    control_cert_fingerprint = env_optional("CONTROL_CERT_FINGERPRINT").lower() or None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id
        FROM servers
        WHERE agent_url = ?
          AND name = ?
          AND port = ?
          AND subscription_group = ?
        """,
        (agent_url, server_name, server_port, subscription_group),
    )
    row = cur.fetchone()
    if row is None:
        created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
        cur.execute(
            """
            INSERT INTO servers (
                name, flag, host, port, public_key, short_id, agent_url, agent_token,
                access_mode, subscription_group, is_active, tcp_port, country_code,
                display_order, control_mode, control_token_hash,
                control_cert_fingerprint, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server_name,
                server_flag,
                server_host,
                server_port,
                public_key,
                short_id,
                agent_url,
                agent_token,
                access_mode,
                subscription_group,
                is_active,
                tcp_port,
                country_code,
                display_order,
                control_mode,
                control_token_hash,
                control_cert_fingerprint,
                created_at,
            ),
        )
        server_id = cur.lastrowid
    else:
        server_id = row[0]
        cur.execute(
            """
            UPDATE servers
            SET name = ?, flag = ?, host = ?, port = ?, public_key = ?, short_id = ?,
                agent_token = ?, access_mode = ?, subscription_group = ?, is_active = ?,
                tcp_port = ?,
                country_code = COALESCE(?, country_code),
                display_order = ?,
                control_mode = ?,
                control_token_hash = COALESCE(?, control_token_hash),
                control_cert_fingerprint = COALESCE(?, control_cert_fingerprint)
            WHERE id = ?
            """,
            (
                server_name,
                server_flag,
                server_host,
                server_port,
                public_key,
                short_id,
                agent_token,
                access_mode,
                subscription_group,
                is_active,
                tcp_port,
                country_code,
                display_order,
                control_mode,
                control_token_hash,
                control_cert_fingerprint,
                server_id,
            ),
        )

    cur.execute(
        """
        SELECT id, user_id, client_uuid
        FROM subscriptions
        WHERE is_active = 1
        """
    )
    subscriptions = cur.fetchall()

    synced = 0
    for subscription_id, user_id, client_uuid in subscriptions:
        email = f"user_{user_id}_sub_{subscription_id}"
        push_enabled = control_mode in {"push", "observe"}
        is_synced = (
            1
            if push_enabled
            and add_client(agent_url, agent_token, client_uuid, email)
            else 0
        )
        # Push the sub's per-device UUIDs too. Each device fetches its OWN
        # /sub/<device-uuid>; a node that never got the device UUID 404s that
        # fetch and the LOCATION is silently dropped from that device's config.
        # Mirrors the bot's sync_subscription_to_servers.
        for dev_id, dev_uuid in cur.execute(
            "SELECT id, uuid FROM devices WHERE subscription_id = ? "
            "AND is_active = 1 AND is_suspended = 0",
            (subscription_id,),
        ).fetchall():
            if push_enabled:
                add_client(agent_url, agent_token, dev_uuid, f"{email}_dev_{dev_id}")
        created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
        cur.execute(
            """
            INSERT INTO subscription_servers (subscription_id, server_id, is_synced, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subscription_id, server_id) DO UPDATE SET is_synced = excluded.is_synced
            """,
            (subscription_id, server_id, is_synced, created_at),
        )
        synced += is_synced

    conn.commit()
    conn.close()
    print(f"registered server_id={server_id}, synced={synced}, total={len(subscriptions)}")


if __name__ == "__main__":
    main()
