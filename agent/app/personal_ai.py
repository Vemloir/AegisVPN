"""Optional, node-local ISP test for exactly one authenticated subscription."""

import copy
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .gemini_policy import DOMAINS as GEMINI_DOMAINS

PROFILE = Path("/data/node-control/personal-ai.json")
TAG = "personal-ai-isp"
# Match product hosts, not shared identity/social networks wholesale.
DOMAINS = (
    "domain:chatgpt.com",
    "domain:oaistatic.com",
    "domain:oaiusercontent.com",
    "full:auth.openai.com",
    "full:auth0.openai.com",
    "full:api.openai.com",
    "domain:claude.ai",
    "full:claude.com",
    "domain:anthropic.com",
    "domain:grok.com",
    "full:grok.x.ai",
    "full:api.x.ai",
    "domain:meta.ai",
    *("full:" + d.removeprefix("domain:") for d in GEMINI_DOMAINS if d.startswith("domain:")),
)


def validate(profile: dict) -> dict:
    def valid_host(host):
        try:
            return ipaddress.ip_address(host).is_global and not ipaddress.ip_address(host).is_multicast
        except ValueError:
            return bool(
                re.fullmatch(r"(?=.{3,253}$)(?:[a-zA-Z0-9-]+\.)+[a-zA-Z][a-zA-Z0-9-]*", host)
            ) and not host.lower().endswith((".localhost", ".local", ".internal", ".home.arpa", ".test", ".invalid"))

    try:
        user, subscription, port = (profile[k] for k in ("user_id", "subscription_id", "port"))
        if any(type(n) is not int or n < 1 for n in (user, subscription, port)) or port > 65535:
            raise ValueError
        address, username, password = (profile[k] for k in ("address", "username", "password"))
        if not isinstance(address, str) or not valid_host(address):
            raise ValueError
        if not all(
            isinstance(s, str) and 0 < len(s.encode()) <= 255 and not any(ord(c) < 32 or ord(c) == 127 for c in s)
            for s in (username, password)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid node-local personal AI profile") from None
    return {
        "user_id": user,
        "subscription_id": subscription,
        "address": address,
        "port": port,
        "username": username,
        "password": password,
    }


def from_url(url: str, user: int, subscription: int) -> dict:
    try:
        u = urlsplit(url.strip())
        if (
            u.scheme not in {"socks5", "socks5h"}
            or u.path not in ("", "/")
            or u.query
            or u.fragment
            or u.username is None
            or u.password is None
        ):
            raise ValueError
        return validate(
            {
                "user_id": user,
                "subscription_id": subscription,
                "address": u.hostname,
                "port": u.port,
                "username": unquote(u.username, errors="strict"),
                "password": unquote(u.password, errors="strict"),
            }
        )
    except (TypeError, ValueError):
        raise ValueError("Expected a valid SOCKS5 proxy URL") from None


def load_profile() -> dict | None:
    try:
        return validate(json.loads(PROFILE.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError("Cannot read node-local personal AI profile") from None


def apply(config: dict, profile: dict | None) -> bool:
    if (
        profile is None
        and not any(o.get("tag") == TAG for o in config.get("outbounds", []))
        and not any(r.get("ruleTag") == TAG for r in config.get("routing", {}).get("rules", []))
    ):
        return False
    candidate = copy.deepcopy(config)
    outbounds = candidate["outbounds"]
    rules = candidate["routing"]["rules"]
    position = next((i for i, o in enumerate(outbounds) if o.get("tag") == TAG), None)
    outbounds[:] = [o for o in outbounds if o.get("tag") != TAG]
    rules[:] = [r for r in rules if r.get("ruleTag") != TAG]
    if profile is not None:
        p = validate(profile)
        index = next((i for i, r in enumerate(rules) if "domain:gemini.google.com" in r.get("domain", [])), None)
        if index is None or any(
            i >= index and ("geoip:private" in r.get("ip", []) or "geosite:private" in r.get("domain", []))
            for i, r in enumerate(rules)
        ):
            raise ValueError("Expected Gemini rules after private-network guards")
        inbounds = sorted(
            i["tag"] for i in candidate.get("inbounds", []) if i.get("protocol") == "vless" and i.get("tag")
        )
        if not inbounds:
            raise ValueError("No authenticated VLESS inbounds")
        identity = f"regexp:^user_{p['user_id']}_sub_{p['subscription_id']}(?:_dev_[1-9][0-9]*)?(?:_cdn)?$"
        match = {
            "type": "field",
            "ruleTag": TAG,
            "inboundTag": inbounds,
            "user": [identity],
            "domain": list(DOMAINS),
            "port": "443",
        }
        rules[index:index] = [
            {**match, "network": "udp", "outboundTag": "block"},
            {**match, "network": "tcp", "outboundTag": TAG},
        ]
        # A reapply must not reorder existing outbounds. In particular, this
        # opt-in proxy must never become Xray's default (first outbound).
        if not outbounds:
            raise ValueError("Expected an existing default outbound")
        outbounds.insert(
            position or len(outbounds),
            {
                "tag": TAG,
                "protocol": "socks",
                "settings": {
                    "servers": [
                        {
                            "address": p["address"],
                            "port": p["port"],
                            "users": [{"user": p["username"], "pass": p["password"]}],
                        },
                    ]
                },
            }
        )
    if candidate == config:
        return False
    config.clear()
    config.update(candidate)
    return True
