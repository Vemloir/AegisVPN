import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DB_PATH = "/root/aegis/deploy/vps/data/bot/aegis.db"


def post_json(url: str, token: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python resync_server_by_id.py <server_id>")

    server_id = int(sys.argv[1])

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    server = cur.execute(
        """
        select id, name, agent_url, agent_token
        from servers
        where id = ? and is_active = 1
        """,
        (server_id,),
    ).fetchone()
    if server is None:
        raise SystemExit(f"active server not found: {server_id}")

    subs = cur.execute(
        """
        select id, user_id, client_uuid
        from subscriptions
        where is_active = 1
        order by id
        """
    ).fetchall()

    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    ok_count = 0
    for sub in subs:
        email = f"user_{sub['user_id']}_sub_{sub['id']}"
        status, body = post_json(
            f"{server['agent_url'].rstrip('/')}/client/add",
            server["agent_token"],
            {"uuid": sub["client_uuid"], "email": email, "expire_ms": 0},
        )
        is_synced = 1 if status == 200 else 0
        ok_count += is_synced
        # Push per-device UUIDs too — without them a device's /sub/<uuid> 404s on
        # this node and the location is silently dropped from that device's config
        # (the Germany bug). Mirrors the bot's sync_subscription_to_servers.
        for dev in cur.execute(
            "select id, uuid from devices where subscription_id = ? "
            "and is_active = 1 and is_suspended = 0",
            (sub["id"],),
        ).fetchall():
            post_json(
                f"{server['agent_url'].rstrip('/')}/client/add",
                server["agent_token"],
                {"uuid": dev["uuid"], "email": f"{email}_dev_{dev['id']}", "expire_ms": 0},
            )
        cur.execute(
            """
            INSERT INTO subscription_servers (subscription_id, server_id, is_synced, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subscription_id, server_id) DO UPDATE SET is_synced = excluded.is_synced
            """,
            (sub["id"], server["id"], is_synced, created_at),
        )
        if not is_synced:
            print(
                f"failed sub={sub['id']} uuid={sub['client_uuid']} "
                f"status={status} body={body[:160]!r}"
            )

    conn.commit()
    print(f"server={server['id']} name={server['name']} synced={ok_count}/{len(subs)}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
