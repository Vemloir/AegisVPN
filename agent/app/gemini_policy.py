"""Explicit admin Gemini policy. No policy means preserve the node's legacy setup."""

import copy
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

POLICY_PATH = Path("/data/node-control/gemini-policy.json")
DOT_PORT = 18553
# Keep aligned with the legacy boot policy, including mobile/API backends.
DOMAINS = [
    "geosite:google-gemini",
    "domain:gemini.google.com",
    "domain:bard.google.com",
    "domain:generativelanguage.googleapis.com",
    "domain:robinfrontend-pa.googleapis.com",
    "domain:proactivebackend-pa.googleapis.com",
    "domain:alkalimakersuite-pa.clients6.google.com",
    "domain:waa-pa.clients6.google.com",
    "domain:signaler-pa.clients6.google.com",
    "domain:ogads-pa.clients6.google.com",
    "domain:apis.google.com",
    "domain:aistudio.google.com",
    "domain:ai.google.dev",
    "domain:cloudcode-pa.googleapis.com",
    "domain:notebooklm.google.com",
    "domain:notebooklm.google",
    "domain:labs.google",
    "domain:aisandbox-pa.googleapis.com",
    "domain:aitestkitchen.withgoogle.com",
    "domain:stitch.withgoogle.com",
    "domain:antigravity.google",
]


def validate_policy(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid Gemini policy")
    mode = value.get("mode")
    if mode == "warp":
        if set(value) != {"mode", "ip_version"} or value["ip_version"] not in ("ipv4", "ipv6"):
            raise ValueError("WARP: выберите IPv4 или IPv6")
        return dict(value)
    if mode != "dns" or set(value) != {"mode", "resolver"}:
        raise ValueError("Выберите DNS или WARP")
    address = value.get("resolver")
    if (
        not isinstance(address, str)
        or not address
        or len(address) > 255
        or any(c.isspace() or ord(c) < 32 for c in address)
    ):
        raise ValueError("Укажите адрес DoH (https://…) или DoT (tls://…)")
    try:
        parts = urlsplit(address)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Некорректный адрес резолвера") from exc
    if (
        parts.scheme not in ("https", "tls")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.query
        or "\\" in address
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("Допустимы https://host/path и tls://host:853 без логина и параметров")
    if parts.scheme == "tls" and parts.path not in ("", "/"):
        raise ValueError("В DoT-адресе не должно быть пути")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Нужен публичный резолвер")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or any(not (c.isascii() and (c.isalnum() or c in ".-")) for c in host):
            raise ValueError("Некорректное имя резолвера") from None
    else:
        if not ip.is_global:
            raise ValueError("Нужен публичный резолвер")
    return {"mode": "dns", "resolver": address}


def capabilities(config_path: str) -> list[str]:
    features = ["gemini-policy-v1"]
    try:
        cfg = json.loads(Path(config_path).read_text())
        if any(o.get("tag") == "warp" and o.get("protocol") == "wireguard" for o in cfg.get("outbounds", [])):
            features.append("gemini-warp-v1")
    except (OSError, ValueError):
        pass
    return features


def load_policy() -> dict | None:
    try:
        value = json.loads(POLICY_PATH.read_text())
    except FileNotFoundError:
        return None
    return validate_policy(value)  # corrupt persisted state must fail closed


def apply_policy(config: dict, value: dict | None) -> bool:
    if value is None:
        return False
    policy = validate_policy(value)
    candidate = copy.deepcopy(config)
    outbounds = candidate.setdefault("outbounds", [])
    if policy["mode"] == "warp" and not any(
        o.get("tag") == "warp" and o.get("protocol") == "wireguard" for o in outbounds
    ):
        raise ValueError("WARP is not configured on this node")
    rules = candidate.setdefault("routing", {}).setdefault("rules", [])
    dns = candidate.setdefault("dns", {})
    servers = dns.setdefault("servers", [])

    def managed(entry, key):
        return entry.get("ruleTag") != "gemini-admin-proxy" and "domain:gemini.google.com" in entry.get(key, [])

    domains = list(DOMAINS)
    for rule in rules:
        if managed(rule, "domain"):
            domains.extend(d for d in rule["domain"] if d not in domains)
    indices = [i for i, rule in enumerate(rules) if managed(rule, "domain")]
    if indices:
        index = indices[0]
    else:
        index = 0
        for rule in rules:
            if (
                rule.get("inboundTag")
                or "geoip:private" in rule.get("ip", [])
                or "geosite:private" in rule.get("domain", [])
            ):
                index += 1
            else:
                break
    rules[:] = [r for r in rules if not managed(r, "domain")]
    servers[:] = [s for s in servers if not (isinstance(s, dict) and managed(s, "domains"))]
    outbounds[:] = [o for o in outbounds if o.get("tag") != "gemini"]
    outbound = {"tag": "gemini", "protocol": "freedom", "settings": {"domainStrategy": "ForceIPv4"}}
    if policy["mode"] == "warp":
        family = "ForceIPv6" if policy["ip_version"] == "ipv6" else "ForceIPv4"
        outbound["settings"]["domainStrategy"] = family
        outbound["streamSettings"] = {"sockopt": {"dialerProxy": "warp"}}
        if family == "ForceIPv6":
            dns["queryStrategy"] = "UseIP"
        # Do NOT change the shared WireGuard identity/strategy or Microsoft's adapter.
    else:
        address = policy["resolver"]
        address = (
            "https+local://" + address.removeprefix("https://")
            if address.startswith("https://")
            else f"tcp+local://127.0.0.1:{DOT_PORT}"
        )
        dns["disableFallbackIfMatch"] = True
        servers.insert(0, {"address": address, "domains": domains, "skipFallback": True, "queryStrategy": "UseIPv4"})
    outbounds.append(outbound)
    rules[index:index] = [
        {"type": "field", "network": "udp", "domain": domains, "outboundTag": "block"},
        {"type": "field", "domain": domains, "outboundTag": "gemini"},
    ]
    for inbound in candidate.get("inbounds", []):
        sniffing = inbound.get("sniffing") or {}
        if "tls" in sniffing.get("destOverride", []) and not sniffing.get("routeOnly"):
            sniffing["destOverride"] = list(dict.fromkeys([*sniffing["destOverride"], "quic"]))
    if candidate == config:
        return False
    config.clear()
    config.update(candidate)
    return True
