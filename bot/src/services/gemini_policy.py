"""Admin node-scoped Gemini configuration and capability-gated publication."""

import ipaddress
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Server

CAPABILITY = "gemini-policy-v1"


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


def supported(server: Server) -> bool:
    return server.control_mode == "pull" and CAPABILITY in (server.control_capabilities or {}).get("features", [])


DECODO_IP_CAPABILITY = "gemini-admin-proxy-ip-v1"
DECODO_PORTS = range(10001, 10011)


def decodo_port(server: Server) -> int:
    port = (server.gemini_preferences or {}).get("decodo_port")
    return port if type(port) is int and port in DECODO_PORTS else DECODO_PORTS.start


async def set_policy(
    session: AsyncSession,
    server_id: int,
    policy: dict,
    expected: dict | None,
    *,
    expected_admin_proxy: bool | None = None,
    expected_port: int | None = None,
) -> Server:
    from src.services.node_control_service import NodeControlService

    policy = validate_policy(policy)
    # Serialize concurrent admins (including SQLite), and reject stale forms.
    await session.execute(
        update(Server).where(Server.id == server_id).values(desired_generation=Server.desired_generation)
    )
    server = (
        await session.execute(select(Server).where(Server.id == server_id).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if server is None:
        raise ValueError("Локация не найдена")
    if not server.is_active or not supported(server):
        raise ValueError("Нужны активная локация, pull-режим и обновлённый агент Gemini")
    if (
        server.gemini_policy != expected
        or (expected_admin_proxy is not None and bool(server.gemini_admin_proxy) != expected_admin_proxy)
        or (expected_port is not None and decodo_port(server) != expected_port)
    ):
        raise ValueError("Настройки уже изменены. Откройте Gemini заново")
    if policy["mode"] == "warp" and "gemini-warp-v1" not in (server.control_capabilities or {}).get("features", []):
        raise ValueError("На ноде не подтверждён настроенный WARP")
    prefs = dict(server.gemini_preferences or {})
    if server.gemini_policy:
        prefs[server.gemini_policy["mode"]] = dict(server.gemini_policy)
    prefs[policy["mode"]] = dict(policy)
    server.gemini_preferences = prefs
    server.gemini_admin_proxy = False
    server.gemini_policy = policy
    await session.flush()
    await NodeControlService.publish_for_servers(session, {server.id})
    server.gemini_policy_generation = server.desired_generation
    return server


async def set_admin_proxy(
    session: AsyncSession,
    server_id: int,
    expected: dict | None,
    expected_admin_proxy: bool,
    *,
    port: int | None = None,
    expected_port: int | None = None,
) -> Server:
    from src.core.config import settings
    from src.services.node_control_service import NodeControlService

    await session.execute(
        update(Server).where(Server.id == server_id).values(desired_generation=Server.desired_generation)
    )
    server = (
        await session.execute(select(Server).where(Server.id == server_id).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if server is None or not server.is_active or not supported(server):
        raise ValueError("Локация недоступна для настройки Gemini")
    if len(settings.admin_ids) != 1 or "gemini-admin-proxy-v1" not in (server.control_capabilities or {}).get(
        "features", []
    ):
        raise ValueError("Decodo не подготовлен на этой ноде")
    if port is not None and (
        DECODO_IP_CAPABILITY not in (server.control_capabilities or {}).get("features", [])
        or type(port) is not int
        or port not in DECODO_PORTS
    ):
        raise ValueError("Этот IP Decodo недоступен на ноде")
    if (
        server.gemini_policy != expected
        or bool(server.gemini_admin_proxy) != expected_admin_proxy
        or (expected_port is not None and decodo_port(server) != expected_port)
    ):
        raise ValueError("Настройки уже изменены. Откройте Gemini заново")
    if port is not None:
        server.gemini_preferences = {**(server.gemini_preferences or {}), "decodo_port": port}
    server.gemini_admin_proxy = True
    await session.flush()
    await NodeControlService.publish_for_servers(session, {server.id})
    server.gemini_policy_generation = server.desired_generation
    return server
