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
from .xray import api_server, normalize_online_name, run_xray_api

# Whether any IPs were blocked in the previous cycle. Used to avoid
# calling sib (which reads stdin and spams errors) when there's nothing to do.
_prev_had_excess: bool = False

# Per-user simultaneous-IP overrides, keyed by the bot's user id (the same id
# embedded in the Xray email `user_<id>_sub_...`). Value semantics: 0 = unlimited,
# >0 = that many IPs. A missing key falls back to the node default
# (`settings.conn_limit`). Persisted so it survives node/agent restarts.
_OVERRIDES_PATH = "/data/conn_limits.json"
_overrides: dict[int, int] = {}


class StatsQueryError(RuntimeError):
    """The local Xray API did not provide a complete authoritative sample."""


def _load_overrides() -> None:
    global _overrides
    try:
        with open(_OVERRIDES_PATH) as fh:
            raw = json.load(fh)
        _overrides = {int(k): int(v) for k, v in raw.items()}
    except (OSError, ValueError, TypeError):
        _overrides = {}


def _write_overrides(values: dict[int, int]) -> None:
    os.makedirs(os.path.dirname(_OVERRIDES_PATH), exist_ok=True)
    tmp = _OVERRIDES_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump({str(k): v for k, v in values.items()}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, _OVERRIDES_PATH)
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def _save_overrides() -> None:
    try:
        _write_overrides(_overrides)
    except OSError as exc:
        print(f"conn-limit: failed to persist overrides: {exc}")


def set_override(user_id: int, limit: int | None) -> None:
    """Set (or, when limit is None, clear) a user's connection-limit override."""
    if limit is None:
        _overrides.pop(user_id, None)
    else:
        _overrides[user_id] = max(0, int(limit))
    _save_overrides()


def replace_overrides(overrides: dict[int, int]) -> None:
    """Atomically replace the complete desired override set.

    Unlike individual legacy pushes, a pull snapshot is authoritative: an
    override absent from it must be removed rather than retained indefinitely.
    Persist before swapping the in-memory map so a failed write cannot be
    acknowledged as applied.
    """
    normalized = {int(user_id): max(0, int(limit)) for user_id, limit in overrides.items()}
    _write_overrides(normalized)
    global _overrides
    _overrides = normalized


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
        raise StatsQueryError("online user query failed")
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        raise StatsQueryError("online user query returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise StatsQueryError("online user query returned an invalid payload")
    names = payload.get("users", [])
    if not isinstance(names, (list, dict)):
        raise StatsQueryError("online user query returned an invalid users shape")
    emails: list[str] = []
    for name in names:
        email = normalize_online_name(str(name))
        if email:
            emails.append(email)
    return emails


async def online_ips(email: str) -> dict[str, int]:
    """{source_ip: last_seen_unix} for a user's live sessions."""
    rc, out = await run_xray_api(["statsonlineiplist", f"--server={api_server()}", "-email", email])
    if rc != 0:
        raise StatsQueryError("online IP query failed")
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        raise StatsQueryError("online IP query returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise StatsQueryError("online IP query returned an invalid payload")
    ips = payload.get("ips", {})
    if not isinstance(ips, dict):
        raise StatsQueryError("online IP query returned an invalid ips shape")
    try:
        return {str(ip): int(last_seen) for ip, last_seen in ips.items()}
    except (TypeError, ValueError):
        raise StatsQueryError("online IP query returned invalid timestamps") from None


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
