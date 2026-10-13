"""CDN fallback paths: how a client reaches a node through a CDN resource.

When a mobile operator switches to its allowlist, only addresses on the list
stay reachable; a node's own IP is not one of them, but the edges of a large
Russian CDN are. A CDN resource whose origin is the node's CDN inbound (VLESS +
XHTTP behind ordinary TLS) therefore keeps the node usable: the phone talks to
the edge, the edge talks to the node.

The CDN path is a fallback, never the default. Every location keeps its direct
REALITY outbound as the primary; the client health-checks it and switches to the
CDN tier only while it is down. That keeps CDN traffic (billed per gigabyte)
to the hours an allowlist is actually in force.
"""

from __future__ import annotations

import copy
import json
import secrets
from collections import defaultdict
from urllib.parse import quote, urlencode

import aiohttp
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import CdnEndpoint, Server

CDN_CAPABILITY = "cdn-v1"
CDN_TAG_PREFIX = "cdn-"
CDN_BALANCER_TAG = "cdn"
CDN_LOOPBACK_INBOUND = "cdn-fallback-in"
CDN_LOOPBACK_OUTBOUND = "cdn-fallback"
PRIMARY_BALANCER_TAG = "primary"
_PROBE_URL = "http://www.gstatic.com/generate_204"
# Through a CDN the upload must be a sequence of ordinary requests: a CDN
# buffers or rejects a streaming request body, which stream-up/stream-one need.
_CDN_MODE = "packet-up"
# How XHTTP must look to pass a Russian CDN (CDNvideo behind Beget/Timeweb).
# Its edge forwards only file-like paths, so the session id and sequence go in
# the query instead of the path; the session id is not a UUID and padding
# rides in a cookie, not a URL-shaped Referer, since its anti-proxy filter
# keys on both. The node's CDN inbound uses the same profile
# (CDN_XHTTP_PROFILE in agent/entrypoint.sh); both sides must match.
CDN_XHTTP_PROFILE = {
    "sessionIDPlacement": "query",
    "sessionIDKey": "v",
    "sessionIDTable": "Base62",
    "sessionIDLength": "16-24",
    "seqPlacement": "query",
    "seqKey": "n",
    "xPaddingObfsMode": True,
    "xPaddingPlacement": "cookie",
    "xPaddingKey": "sid",
    "xPaddingMethod": "tokenish",
}


def serves_cdn(server: Server) -> bool:
    """The node runs a CDN origin inbound clients may be sent to.

    A pull-mode node reports what its agent really serves; a node that has
    reported capabilities but not this one must not be advertised, whatever
    its row says. Push-mode nodes report nothing, so the row is trusted.
    """
    if not (server.cdn_port and (server.cdn_path or "").strip() and (server.cdn_origin_host or "").strip()):
        return False
    capabilities = server.control_capabilities
    if capabilities is None:
        return True
    return CDN_CAPABILITY in set(capabilities.get("features") or [])


async def endpoints_by_server(
    session: AsyncSession,
    servers: list[Server],
) -> dict[int, list[CdnEndpoint]]:
    """Advertisable CDN resources for each CDN-capable server.

    A resource the reachability check found broken is withheld: a dead
    fallback is worse than none, because the client would switch to it at
    exactly the moment the direct path is gone. Never-checked ones are kept.
    """
    eligible = {server.id for server in servers if serves_cdn(server)}
    if not eligible:
        return {}
    rows = (
        (
            await session.execute(
                select(CdnEndpoint)
                .where(
                    CdnEndpoint.server_id.in_(eligible),
                    CdnEndpoint.enabled == True,  # noqa: E712
                    or_(CdnEndpoint.last_check_ok.is_(None), CdnEndpoint.last_check_ok == True),  # noqa: E712
                )
                .order_by(CdnEndpoint.position, CdnEndpoint.id)
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[int, list[CdnEndpoint]] = defaultdict(list)
    for row in rows:
        grouped[row.server_id].append(row)
    return dict(grouped)


async def probe_endpoint(server: Server, endpoint: CdnEndpoint, *, timeout: float = 10.0) -> tuple[bool, str]:
    """Does a request through this CDN resource reach the node's Xray?

    A request under the inbound's path always comes back stamped with padding
    by Xray itself, whatever the status (a bare GET is a 400 to it): a cookie
    named after the profile's padding key, or an ``X-Padding`` header from an
    inbound without the CDN profile. The CDN cannot produce either, so its
    presence proves the whole chain — edge, resource, origin certificate,
    inbound — while a 5xx without it is the CDN failing to reach the origin.
    That failure is exactly what a client-side ping hides: the direct path
    answers, so a dead fallback is invisible until the allowlist switches on.
    """
    # The inbound's own path: the CDN refuses anything not shaped like a file.
    url = f"https://{endpoint.connect_address}{server.cdn_path or '/'}?_={secrets.token_hex(4)}"
    headers = {"Host": endpoint.host, "Cache-Control": "no-cache", "User-Agent": "Mozilla/5.0"}
    padding_cookie = CDN_XHTTP_PROFILE["xPaddingKey"] + "="
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.get(
                url,
                headers=headers,
                server_hostname=endpoint.tls_server_name,
                allow_redirects=False,
            ) as response:
                cookies = response.headers.getall("Set-Cookie", [])
                if "X-Padding" in response.headers or any(c.startswith(padding_cookie) for c in cookies):
                    return True, f"origin reached (HTTP {response.status})"
                return False, f"HTTP {response.status} without the origin's marker"
    except Exception as exc:  # noqa: BLE001 - any failure is a failed check
        return False, type(exc).__name__


def _xhttp_settings(server: Server, endpoint: CdnEndpoint) -> dict:
    settings: dict = {
        "host": endpoint.host,
        "path": server.cdn_path,
        "mode": _CDN_MODE,
        **copy.deepcopy(CDN_XHTTP_PROFILE),
        "xmux": {"hKeepAlivePeriod": 15},
    }
    if endpoint.xhttp_extra:
        settings["extra"] = copy.deepcopy(endpoint.xhttp_extra)
    return settings


def build_outbound(server: Server, endpoint: CdnEndpoint, client_id: str, tag: str) -> dict:
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": endpoint.connect_address,
                    "port": 443,
                    "users": [{"id": client_id, "encryption": "none", "level": 8}],
                }
            ]
        },
        "streamSettings": {
            "network": "xhttp",
            "security": "tls",
            # The edge presents the CDN's own valid certificate for the resource
            # (or for the fronting name), so normal verification stays on.
            "tlsSettings": {
                "serverName": endpoint.tls_server_name,
                "alpn": ["h2", "http/1.1"],
                "fingerprint": "chrome",
            },
            "xhttpSettings": _xhttp_settings(server, endpoint),
            "sockopt": {"tcpKeepAliveIdle": 10, "tcpKeepAliveInterval": 5},
        },
    }


def build_link(server: Server, endpoint: CdnEndpoint, client_id: str, label: str) -> str:
    """A vless:// link for clients that only take a link list."""
    query = [
        ("type", "xhttp"),
        ("security", "tls"),
        ("encryption", "none"),
        ("sni", endpoint.tls_server_name),
        ("host", endpoint.host),
        ("path", server.cdn_path or "/"),
        ("mode", _CDN_MODE),
        ("alpn", "h2,http/1.1"),
        ("fp", "chrome"),
    ]
    # A link has no fields for the placements, so they travel in "extra".
    extra = {**CDN_XHTTP_PROFILE, **(endpoint.xhttp_extra or {})}
    query.append(("extra", json.dumps(extra, separators=(",", ":"))))
    return f"vless://{client_id}@{endpoint.connect_address}:443?{urlencode(query, quote_via=quote)}#{quote(label)}"


def client_id_of(config: dict) -> str:
    """The credential the location's primary outbound authenticates with."""
    proxy = config["outbounds"][0]
    for server in proxy.get("settings", {}).get("vnext", []):
        for user in server.get("users", []):
            if user.get("id"):
                return str(user["id"])
    # Hysteria2 authenticates with the same UUID.
    return str(proxy.get("streamSettings", {}).get("hysteriaSettings", {}).get("auth") or "")


def attach_fallback(config: dict, cdn_outbounds: list[dict]) -> dict:
    """Return ``config`` with a CDN tier behind its primary outbound(s).

    Xray's balancer cannot fall back to another balancer, only to an outbound,
    so the fallback is a loopback outbound feeding a local inbound whose traffic
    is routed to the CDN balancer. The primary is health-checked; while it
    answers, the CDN tier carries nothing.
    """
    if not cdn_outbounds:
        return config
    result = copy.deepcopy(config)
    outbounds = result["outbounds"]
    # Keep the primary first (it stays the default outbound), CDN right after.
    insert_at = 1 + sum(1 for outbound in outbounds[1:] if str(outbound.get("tag", "")).startswith("proxy"))
    outbounds[insert_at:insert_at] = copy.deepcopy(cdn_outbounds)
    outbounds.append(
        {"tag": CDN_LOOPBACK_OUTBOUND, "protocol": "loopback", "settings": {"inboundTag": CDN_LOOPBACK_INBOUND}}
    )
    result["inbounds"] = [
        *result.get("inbounds", []),
        {
            "tag": CDN_LOOPBACK_INBOUND,
            "listen": "127.0.0.1",
            "port": 0,
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1", "network": "tcp,udp"},
        },
    ]

    routing = result.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    balancers = routing.setdefault("balancers", [])
    if balancers:
        # The auto-select entry already balances its primaries.
        for balancer in balancers:
            balancer["fallbackTag"] = CDN_LOOPBACK_OUTBOUND
    else:
        balancers.append(
            {
                "tag": PRIMARY_BALANCER_TAG,
                "selector": ["proxy"],
                "strategy": {"type": "leastPing"},
                "fallbackTag": CDN_LOOPBACK_OUTBOUND,
            }
        )
        rules.append({"type": "field", "network": "tcp,udp", "balancerTag": PRIMARY_BALANCER_TAG})
        result["burstObservatory"] = {
            "subjectSelector": ["proxy"],
            "pingConfig": {
                "destination": _PROBE_URL,
                "connectivity": "",
                # Switch within ~30s of the direct path going dark.
                "interval": "15s",
                "sampling": 2,
                "timeout": "3s",
            },
        }
    # No observatory for the CDN tier: it is only used while the primary is
    # down, and random needs no measurements to pick a working resource.
    balancers.append({"tag": CDN_BALANCER_TAG, "selector": [CDN_TAG_PREFIX], "strategy": {"type": "random"}})
    # First rule: traffic re-entering through the loopback goes to the CDN tier
    # (it already passed the direct-routing rules on its first pass).
    rules.insert(0, {"type": "field", "inboundTag": [CDN_LOOPBACK_INBOUND], "balancerTag": CDN_BALANCER_TAG})
    return result
