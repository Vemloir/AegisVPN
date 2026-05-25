"""Per-subscription simultaneous-connection limit.

Xray's StatsService reports each user's currently-online source IPs; we keep the
N most-recently-seen and block the overflow with a RoutingService source-IP
block rule, rebuilt every cycle so IPs that drop below the limit are released.
"""

import asyncio
import json

from .config import settings
from .xray import api_server, run_xray_api


async def online_users() -> list[str]:
    """Emails of users with at least one live session right now."""
    rc, out = await run_xray_api(["statsgetallonlineusers", f"--server={api_server()}"])
    if rc != 0:
        return []
    try:
        names = json.loads(out or "{}").get("users", []) or []
    except json.JSONDecodeError:
        return []
    emails = []
    for n in names:  # "user>>>EMAIL>>>online"
        parts = n.split(">>>")
        if len(parts) >= 3 and parts[0] == "user":
            emails.append(parts[1])
    return emails


async def online_ips(email: str) -> dict[str, int]:
    """{source_ip: last_seen_unix} for a user's live sessions."""
    rc, out = await run_xray_api(["statsonlineiplist", f"--server={api_server()}", "-email", email])
    if rc != 0:
        return {}
    try:
        return json.loads(out or "{}").get("ips", {}) or {}
    except json.JSONDecodeError:
        return {}


async def enforce_conn_limit_once() -> int:
    """Block source IPs that exceed the per-subscription limit.

    For each online user with more than `conn_limit` distinct IPs, keep the
    `conn_limit` most-recently-seen IPs and block the rest. The block list is
    rebuilt every cycle with `sib -reset`, so an IP that stops being excess is
    automatically unblocked next cycle. Returns the count of blocked IPs.
    """
    limit = settings.conn_limit
    if limit <= 0:
        return 0
    excess: list[str] = []
    for email in await online_users():
        ips = await online_ips(email)
        if len(ips) <= limit:
            continue
        # keep the `limit` newest by timestamp, block the older overflow
        ordered = sorted(ips.items(), key=lambda kv: kv[1], reverse=True)
        excess.extend(ip for ip, _ in ordered[limit:])

    # rebuild the conn-limit block rule with the full current overflow set
    args = ["sib", f"--server={api_server()}", "-outbound=block", "-ruletag=conn-limit", "-reset", *excess]
    rc, out = await run_xray_api(args)
    if rc != 0:
        print(f"sib failed: {out.strip()}")
    elif excess:
        print(f"conn-limit: blocked {len(excess)} excess IP(s): {excess}")
    return len(excess)


async def conn_limit_loop() -> None:
    interval = max(15, settings.conn_limit_interval)
    while True:
        try:
            await enforce_conn_limit_once()
        except Exception as exc:
            print(f"conn-limit loop error: {exc}")
        await asyncio.sleep(interval)
