"""Per-subscription simultaneous-connection limit.

Xray's StatsService reports each user's currently-online source IPs; we keep the
N most-recently-seen and block the overflow with a RoutingService source-IP
block rule, rebuilt every cycle so IPs that drop below the limit are released.
"""

import asyncio
import json
import os

from . import hysteria
from .config import settings
from .xray import api_server, run_xray_api

# Whether any IPs were blocked in the previous cycle. Used to avoid
# calling sib (which reads stdin and spams errors) when there's nothing to do.
_prev_had_excess: bool = False

# Per-user simultaneous-IP overrides, keyed by the bot's user id (the same id
# embedded in the Xray email `user_<id>_sub_...`). Value semantics: 0 = unlimited,
# >0 = that many IPs. A missing key falls back to the node default
# (`settings.conn_limit`). Persisted so it survives node/agent restarts.
_OVERRIDES_PATH = "/data/conn_limits.json"
_overrides: dict[int, int] = {}


def _load_overrides() -> None:
    global _overrides
    try:
        with open(_OVERRIDES_PATH) as fh:
            raw = json.load(fh)
        _overrides = {int(k): int(v) for k, v in raw.items()}
    except (OSError, ValueError, TypeError):
        _overrides = {}


def _save_overrides() -> None:
    try:
        os.makedirs(os.path.dirname(_OVERRIDES_PATH), exist_ok=True)
        tmp = _OVERRIDES_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({str(k): v for k, v in _overrides.items()}, fh)
        os.replace(tmp, _OVERRIDES_PATH)
    except OSError as exc:
        print(f"conn-limit: failed to persist overrides: {exc}")


def set_override(user_id: int, limit: int | None) -> None:
    """Set (or, when limit is None, clear) a user's connection-limit override."""
    if limit is None:
        _overrides.pop(user_id, None)
    else:
        _overrides[user_id] = max(0, int(limit))
    _save_overrides()


def _user_id_from_email(email: str) -> int | None:
    # Emails look like `user_<id>_sub_<sid>` (optionally `_dev_<did>`).
    parts = email.split("_")
    if len(parts) >= 2 and parts[0] == "user" and parts[1].isdigit():
        return int(parts[1])
    return None


def _limit_for(email: str) -> int:
    uid = _user_id_from_email(email)
    if uid is not None and uid in _overrides:
        return _overrides[uid]
    return settings.conn_limit


_load_overrides()


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
    """Block source IPs that exceed the per-subscription limit, across BOTH
    xray (VLESS) and Hysteria2.

    The limit is enforced over the COMBINED simultaneous sessions of a user:
    xray contributes its distinct online source IPs, Hy2 contributes its live
    session COUNT (Hy2's trafficStats exposes a per-user count, not source IPs).
    For each user whose xray-IPs + Hy2-sessions exceed the limit we:

      * cap xray to ``max(0, limit - hy2_count)`` newest source IPs and block the
        overflow (the block list is rebuilt every cycle with ``sib -reset``, so
        an IP that stops being excess is auto-unblocked next cycle), and
      * kick the user's Hy2 sessions when Hy2 is contributing to the overage —
        Hy2's kick is all-or-nothing per user, so legitimate clients reconnect
        and re-auth, converging back under the limit (xray now leaves room).

    Returns the count of blocked xray IPs.
    """
    global _prev_had_excess
    # Hy2 live-session counts for this cycle (empty when Hy2 is disabled, so on a
    # node without Hysteria2 this is byte-identical to the xray-only behavior).
    hy2_counts = await hysteria.online_counts()
    # Consider every user seen on either protocol (a Hy2-only user with no xray
    # session still counts toward — and can exceed — the limit).
    emails = set(await online_users()) | set(hy2_counts)
    excess: list[str] = []
    hy2_kick: list[str] = []
    for email in emails:
        limit = _limit_for(email)  # per-user override, else node default
        if limit <= 0:  # 0 = unlimited (default disabled or per-user "no limit")
            continue
        hy2_count = hy2_counts.get(email, 0)
        ips = await online_ips(email)
        combined = len(ips) + hy2_count
        if combined <= limit:
            continue
        # Cap xray to whatever the limit leaves after Hy2's slots; block the rest.
        xray_keep = max(0, limit - hy2_count)
        if len(ips) > xray_keep:
            ordered = sorted(ips.items(), key=lambda kv: kv[1], reverse=True)
            excess.extend(ip for ip, _ in ordered[xray_keep:])
        # Hy2 is part of the overage and can't be trimmed per-IP: kick it so the
        # user falls back under the combined limit on reconnect.
        if hy2_count > 0:
            hy2_kick.append(email)

    # Drop the over-limit Hy2 sessions. refresh_from_config already keeps the
    # valid Hy2 user set in sync with the live xray clients, so a kicked user who
    # is still authorized may reconnect — but only up to the limit, since the
    # next cycle re-evaluates the combined count.
    if hy2_kick:
        await hysteria.kick(hy2_kick)

    # Skip sib entirely when nothing was blocked and nothing needs clearing —
    # calling sib with no IPs causes xray to read from stdin and log errors.
    if not excess and not _prev_had_excess:
        return 0

    # rebuild the conn-limit block rule with the full current overflow set
    args = ["sib", f"--server={api_server()}", "-outbound=block", "-ruletag=conn-limit", "-reset", *excess]
    rc, out = await run_xray_api(args)
    if rc != 0:
        print(f"sib failed: {out.strip()}")
    elif excess:
        print(f"conn-limit: blocked {len(excess)} excess IP(s): {excess}")

    _prev_had_excess = bool(excess)
    return len(excess)


async def conn_limit_loop() -> None:
    interval = max(15, settings.conn_limit_interval)
    while True:
        try:
            await enforce_conn_limit_once()
        except Exception as exc:
            print(f"conn-limit loop error: {exc}")
        await asyncio.sleep(interval)
