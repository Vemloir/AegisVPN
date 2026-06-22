"""Local Hysteria2 control plane.

Hysteria2 runs as a separate process and authenticates clients via an HTTP
callback to this agent (:func:`authenticate`). It never re-auths an established
session, so disconnecting a user is a two-step dance: stop authenticating them
(:func:`refresh_from_config` after removal) *then* drop their live QUIC session
(:func:`kick`). Traffic/online stats are read back from Hy2's loopback Stats API
and normalized to xray's shape so the two merge cleanly on the same email key.

Every network call is gated on ``settings.hy2_enabled`` and is best-effort: a
node without Hysteria2 makes zero HTTP calls and these helpers degrade to empty
results rather than raising.
"""

import asyncio
import json
import urllib.error
import urllib.request

from .config import settings
from .xray import get_xray_config, list_vless_inbounds

# uuid -> email, the valid Hy2 user set, derived from the xray vless clients.
# We reuse the client's xray UUID as the Hy2 auth secret and return their email
# as the Hy2 `id`, so Hy2 stats key on the same email as xray.
_clients: dict[str, str] = {}

_TIMEOUT = 3  # seconds; loopback calls, keep short


def refresh_from_config(config: dict) -> None:
    """Rebuild the valid Hy2 user set (uuid -> email) from an xray config."""
    mapping: dict[str, str] = {}
    for inbound in list_vless_inbounds(config):
        for client in inbound.get("settings", {}).get("clients", []):
            uuid = client.get("id")
            email = client.get("email")
            if uuid and email:
                mapping[uuid] = email
    global _clients
    _clients = mapping


async def refresh() -> None:
    """Load the on-disk xray config and rebuild the Hy2 user set from it."""
    config = await get_xray_config()
    refresh_from_config(config)


def authenticate(auth: str) -> dict:
    """Hy2 auth callback result for a given secret (the client's xray UUID)."""
    email = _clients.get(auth)
    if email is None:
        return {"ok": False}
    return {"ok": True, "id": email}


def _request(method: str, path: str, body: bytes | None = None) -> bytes | None:
    """Blocking Hy2 Stats API call. Returns the response body or None on error."""
    url = f"{settings.hy2_stats_url}{path}"
    headers = {"Authorization": settings.hy2_stats_secret or ""}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


async def kick(ids: list[str]) -> bool:
    """Force-close the live QUIC sessions of the given Hy2 ids (emails).

    Must follow a :func:`refresh_from_config` that already removed them from the
    valid set — otherwise the client just reconnects. Best-effort; never raises.
    """
    if not settings.hy2_enabled or not ids:
        return True
    body = json.dumps(ids).encode()
    result = await asyncio.to_thread(_request, "POST", "/kick", body)
    return result is not None


async def traffic() -> dict[str, dict[str, int]]:
    """Hy2 per-user traffic, normalized to xray's {email: {uplink, downlink}}."""
    if not settings.hy2_enabled:
        return {}
    raw = await asyncio.to_thread(_request, "GET", "/traffic")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for email, counters in data.items():
        if not isinstance(counters, dict):
            continue
        out[email] = {
            "uplink": int(counters.get("tx", 0) or 0),
            "downlink": int(counters.get("rx", 0) or 0),
        }
    return out


async def online() -> list[str]:
    """Hy2 ids (emails) with at least one live session right now."""
    if not settings.hy2_enabled:
        return []
    raw = await asyncio.to_thread(_request, "GET", "/online")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        return list(data.keys())
    if isinstance(data, list):
        return [str(x) for x in data]
    return []
