"""Admin-only Gemini HTTPS proxy. Secrets stay node-local, never in snapshots."""

import copy
import json
import re
from pathlib import Path

from .gemini_policy import DOMAINS

SECRET_PATH = Path("/data/node-control/gemini-admin-proxy.json")
USERS_PATH = Path("/data/node-control/gemini-admin-users.json")
TAG = "gemini-admin-proxy"
FIRST_PORT = 10001
LAST_PORT = 10010


def validate_port(port: int) -> int:
    if type(port) is not int or not FIRST_PORT <= port <= LAST_PORT:
        raise ValueError("Invalid Decodo IP slot")
    return port


def validate_users(users: list[str]) -> list[str]:
    if len(users) > 1000 or any(
        not re.fullmatch(r"user_[1-9][0-9]*_sub_[1-9][0-9]*(?:_dev_[1-9][0-9]*)?", x) for x in users
    ):
        raise ValueError("Invalid Gemini proxy users")
    return sorted(set(users))


def load_selection() -> tuple[list[str], int | None]:
    try:
        value = json.loads(USERS_PATH.read_text())
    except FileNotFoundError:
        return [], None
    if not isinstance(value, dict) or not isinstance(value.get("users"), list):
        raise ValueError("Invalid persisted Gemini proxy users")
    port = value.get("port")
    return validate_users(value["users"]), validate_port(port) if port is not None else None


def credentials() -> dict:
    value = json.loads(SECRET_PATH.read_text())
    if (
        not isinstance(value, dict)
        or value.get("host") != "dc.decodo.com"
        or value.get("port") != 10001
        or not isinstance(value.get("username"), str)
        or not value["username"]
        or not isinstance(value.get("password"), str)
        or not value["password"]
    ):
        raise ValueError("Invalid node-local Decodo profile")
    return value


def available() -> bool:
    try:
        credentials()
        return True
    except (OSError, ValueError, TypeError):
        return False


def apply(config: dict, users: list[str], port: int | None = None) -> bool:
    users = validate_users(users)
    if port is not None:
        validate_port(port)
    if (
        not users
        and not any(o.get("tag") == TAG for o in config.get("outbounds", []))
        and not any(r.get("ruleTag") == TAG for r in config.get("routing", {}).get("rules", []))
    ):
        return False
    candidate = copy.deepcopy(config)
    rules = candidate.setdefault("routing", {}).setdefault("rules", [])
    rules[:] = [r for r in rules if r.get("ruleTag") != TAG]
    outs = candidate.setdefault("outbounds", [])
    outs[:] = [o for o in outs if o.get("tag") != TAG]
    if users:
        secret = credentials()
        outs.append(
            {
                "tag": TAG,
                "protocol": "http",
                "settings": {
                    "servers": [
                        {
                            "address": secret["host"],
                            "port": secret["port"] if port is None else port,
                            "users": [{"user": secret["username"], "pass": secret["password"]}],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "tls",
                    "tlsSettings": {"serverName": secret["host"], "allowInsecure": False},
                },
            }
        )
        # Keep every private-network/management guard ahead of this exception,
        # but precede the normal Gemini policy. Never grant generic proxy access.
        index = next((i for i, r in enumerate(rules) if "domain:gemini.google.com" in r.get("domain", [])), None)
        if index is None:
            raise ValueError("Missing base Gemini routing policy")
        emails = sorted([*users, *(u + "_cdn" for u in users)])
        rules[index:index] = [
            {
                "ruleTag": TAG,
                "type": "field",
                "user": emails,
                "domain": list(DOMAINS),
                "network": "udp",
                "outboundTag": "block",
            },
            {
                "ruleTag": TAG,
                "type": "field",
                "user": emails,
                "domain": list(DOMAINS),
                "network": "tcp",
                "port": "443",
                "outboundTag": TAG,
            },
        ]
    if candidate == config:
        return False
    config.clear()
    config.update(candidate)
    return True
