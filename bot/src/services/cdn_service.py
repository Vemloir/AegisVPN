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

import asyncio
import copy
import hashlib
import json
import secrets
import ssl
from collections import defaultdict
from urllib.parse import quote, urlencode

import aiohttp
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import CdnEndpoint, Server

CDN_CAPABILITY = "cdn-v1"
CDN_TAG_PREFIX = "cdn-"
CDN_BALANCER_TAG = "cdn"
# Not under CDN_TAG_PREFIX: balancer selectors and the observatory match tags
# by prefix, and a loopback picked as a "CDN path" would send traffic round.
CDN_LOOPBACK_INBOUND = "to-cdn-in"
CDN_LOOPBACK_OUTBOUND = "to-cdn"
PRIMARY_BALANCER_TAG = "primary"
# Auto-select's second stage: any direct node not known to be dead.
PRIMARY_ANY_BALANCER_TAG = "primary-any"
PRIMARY_FALLBACK_INBOUND = "primary-fallback-in"
PRIMARY_FALLBACK_OUTBOUND = "primary-fallback"
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
    *,
    include_preview: bool = False,
) -> dict[int, list[CdnEndpoint]]:
    """Advertisable CDN resources for each CDN-capable server.

    A resource the reachability check found broken is withheld: a dead
    fallback is worse than none, because the client would switch to it at
    exactly the moment the direct path is gone. Never-checked ones are kept,
    except a pinned one, which cannot be described before its certificate is
    known. Preview-only resources come back only with ``include_preview``.
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
        if row.preview_only and not include_preview:
            continue
        if row.pin_cert and not row.cert_pins:
            continue
        grouped[row.server_id].append(row)
    return dict(grouped)


def _unverified_tls() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def edge_cert_sha256(endpoint: CdnEndpoint, *, timeout: float = 10.0) -> str:
    """SHA-256 (hex) of the leaf certificate the edge presents for the endpoint's SNI.

    What a pinned endpoint's clients are told to accept: Xray matches a pinned
    leaf by this hash and skips every other check, the expiry included.
    """
    _reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            endpoint.connect_address,
            443,
            ssl=_unverified_tls(),
            server_hostname=endpoint.tls_server_name,
        ),
        timeout,
    )
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    return hashlib.sha256(der).hexdigest()


def merge_cert_pins(known: list[str], seen: str, *, keep: int = 4) -> str:
    """The pin list after seeing ``seen``: it first, then the most recent others.

    Older pins stay for a while because edges do not switch certificates in
    step, and a client only learns the new list on its next subscription fetch.
    """
    return ",".join([seen, *(pin for pin in known if pin != seen)][:keep])


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
                # A pinned endpoint's certificate is not expected to verify;
                # which certificate it is, is edge_cert_sha256's business.
                ssl=_unverified_tls() if endpoint.pin_cert else True,
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


def _tls_settings(endpoint: CdnEndpoint) -> dict:
    settings: dict = {
        "serverName": endpoint.tls_server_name,
        "alpn": ["h2", "http/1.1"],
        "fingerprint": "chrome",
    }
    if endpoint.pin_cert:
        settings["pinnedPeerCertSha256"] = ",".join(endpoint.cert_pins)
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
            # (or for the fronting name), so normal verification stays on;
            # a pinned endpoint is checked against its learned hashes instead.
            "tlsSettings": _tls_settings(endpoint),
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
    if endpoint.pin_cert:
        query.append(("pcs", ",".join(endpoint.cert_pins)))
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


def cdn_only(config: dict, cdn_outbound: dict, remarks: str) -> dict:
    """``config`` with its direct path replaced by one CDN resource.

    The ``| WL`` preview entry: it behaves exactly like the CDN tier would
    during an allowlist, without waiting for one.
    """
    result = copy.deepcopy(config)
    proxy = copy.deepcopy(cdn_outbound)
    proxy["tag"] = "proxy"
    result["outbounds"][0] = proxy
    result["remarks"] = remarks
    return result


def _cdn_tier_tags(count: int) -> list[str]:
    """Tags for ``count`` CDN paths, none a prefix of another ("cdn-1" would
    also select "cdn-10")."""
    width = len(str(count))
    return [f"{CDN_TAG_PREFIX}{index:0{width}d}" for index in range(1, count + 1)]


def _loopback(tier: int) -> tuple[str, str]:
    """(outbound, inbound) tags of the loopback into CDN tier ``tier``."""
    if tier == 1:
        return CDN_LOOPBACK_OUTBOUND, CDN_LOOPBACK_INBOUND
    return f"{CDN_LOOPBACK_OUTBOUND}-{tier}", f"{CDN_LOOPBACK_OUTBOUND}-{tier}-in"


def _loopback_inbound(tag: str) -> dict:
    return {
        "tag": tag,
        "listen": "127.0.0.1",
        "port": 0,
        "protocol": "dokodemo-door",
        "settings": {"address": "127.0.0.1", "network": "tcp,udp"},
    }


def attach_fallback(config: dict, cdn_outbounds: list[dict]) -> dict:
    """Return ``config`` with CDN tiers behind its primary outbound(s).

    Xray's balancer cannot fall back to another balancer, only to an outbound,
    so each fallback is a loopback outbound feeding a local inbound whose
    traffic is routed to the next balancer. The CDN paths form a chain in the
    order given (the first one first, the next only while it is down); the
    primary is health-checked and, while it answers, no CDN tier carries
    anything.

    Until the first health check has run there is no measurement at all, and a
    leastPing balancer then picks nothing and hands everything to its fallback:
    every fresh connect would start out through the CDN, slower and billed. A
    random balancer instead treats an outbound it has no report on as alive and
    drops only the ones a check found dead, so the direct path is taken until it
    is known to be down. The same holds down the chain.
    """
    if not cdn_outbounds:
        return config
    result = copy.deepcopy(config)
    cdn_outbounds = copy.deepcopy(cdn_outbounds)
    tags = _cdn_tier_tags(len(cdn_outbounds))
    for outbound, tag in zip(cdn_outbounds, tags, strict=True):
        outbound["tag"] = tag
    outbounds = result["outbounds"]
    # Keep the primary first (it stays the default outbound), CDN right after.
    insert_at = 1 + sum(1 for outbound in outbounds[1:] if str(outbound.get("tag", "")).startswith("proxy"))
    outbounds[insert_at:insert_at] = cdn_outbounds
    result["inbounds"] = list(result.get("inbounds", []))
    for tier in range(1, len(tags) + 1):
        out_tag, in_tag = _loopback(tier)
        outbounds.append({"tag": out_tag, "protocol": "loopback", "settings": {"inboundTag": in_tag}})
        result["inbounds"].append(_loopback_inbound(in_tag))

    routing = result.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    balancers = routing.setdefault("balancers", [])
    if balancers:
        # Auto-select keeps ranking its nodes by ping. With no ranking yet, or
        # none alive, it falls to a second stage over the same nodes that takes
        # any one not known to be dead, and only that one falls to the CDN.
        selectors = sorted({selector for balancer in balancers for selector in balancer.get("selector", [])})
        for balancer in balancers:
            balancer["fallbackTag"] = PRIMARY_FALLBACK_OUTBOUND
        outbounds.append(
            {
                "tag": PRIMARY_FALLBACK_OUTBOUND,
                "protocol": "loopback",
                "settings": {"inboundTag": PRIMARY_FALLBACK_INBOUND},
            }
        )
        result["inbounds"].append(_loopback_inbound(PRIMARY_FALLBACK_INBOUND))
        balancers.append(
            {
                "tag": PRIMARY_ANY_BALANCER_TAG,
                "selector": selectors,
                "strategy": {"type": "random"},
                "fallbackTag": CDN_LOOPBACK_OUTBOUND,
            }
        )
        rules.insert(
            0, {"type": "field", "inboundTag": [PRIMARY_FALLBACK_INBOUND], "balancerTag": PRIMARY_ANY_BALANCER_TAG}
        )
    else:
        balancers.append(
            {
                "tag": PRIMARY_BALANCER_TAG,
                "selector": ["proxy"],
                # random, not leastPing: see the docstring.
                "strategy": {"type": "random"},
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
    if len(tags) > 1:
        # A chain only moves on from a CDN path a check found dead, so the paths
        # are checked too. A single path needs no check (it is the only choice)
        # and costs nothing while the primary is up; with several, the checks
        # themselves go through the CDN.
        observatory = result.setdefault("burstObservatory", {"subjectSelector": []})
        observatory["subjectSelector"] = [*observatory.get("subjectSelector", []), CDN_TAG_PREFIX]
    for tier, tag in enumerate(tags, start=1):
        balancer: dict = {
            "tag": CDN_BALANCER_TAG if tier == 1 else f"{CDN_BALANCER_TAG}-tier-{tier}",
            "selector": [tag],
            "strategy": {"type": "random"},
        }
        if tier < len(tags):
            balancer["fallbackTag"] = _loopback(tier + 1)[0]
        balancers.append(balancer)
    # First rules: traffic re-entering through a loopback goes to its CDN tier
    # (it already passed the direct-routing rules on its first pass).
    rules[0:0] = [
        {"type": "field", "inboundTag": [_loopback(tier)[1]], "balancerTag": balancer["tag"]}
        for tier, balancer in enumerate(balancers[-len(tags) :], start=1)
    ]
    return result
