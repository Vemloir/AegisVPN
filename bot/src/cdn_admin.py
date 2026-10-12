"""Operator CLI for CDN fallback paths.

Run inside the bot container, e.g.::

    docker exec -it aegis-bot python -m src.cdn_admin list
    docker exec -it aegis-bot python -m src.cdn_admin origin 3 --host n3.example.ru --port 8443
    docker exec -it aegis-bot python -m src.cdn_admin add 3 --provider Beget --host abc.begetcdn.cloud
    docker exec -it aegis-bot python -m src.cdn_admin check

``origin`` configures the node side (its agent.env must carry the printed
XRAY_CDN_* values); ``add`` registers a CDN resource created by hand in the
provider's panel with that origin. ``check`` probes every resource now, the
same way the scheduler does, so a new resource can be verified on the spot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets

from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import CdnEndpoint, Server
from src.services import cdn_service


async def _list() -> None:
    async with async_session_maker() as session:
        servers = (await session.execute(select(Server).order_by(Server.id))).scalars().all()
        endpoints = (await session.execute(select(CdnEndpoint).order_by(CdnEndpoint.id))).scalars().all()
    by_server: dict[int, list[CdnEndpoint]] = {}
    for endpoint in endpoints:
        by_server.setdefault(endpoint.server_id, []).append(endpoint)
    for server in servers:
        if not server.cdn_port and server.id not in by_server:
            continue
        state = "serves CDN" if cdn_service.serves_cdn(server) else "NOT serving CDN"
        print(
            f"server {server.id} {server.name}: origin {server.cdn_origin_host}:{server.cdn_port}"
            f" path {server.cdn_path} ({state})"
        )
        for endpoint in by_server.get(server.id, []):
            health = {None: "unchecked", True: "ok", False: "BROKEN"}[endpoint.last_check_ok]
            flags = "" if endpoint.enabled else " [disabled]"
            print(
                f"  #{endpoint.id} {endpoint.provider}: {endpoint.host}"
                f" addr={endpoint.connect_address} sni={endpoint.tls_server_name}"
                f" {health} ({endpoint.last_check_detail or '-'}){flags}"
            )


async def _origin(server_id: int, host: str, port: int, path: str | None) -> None:
    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        if server is None:
            raise SystemExit(f"no server {server_id}")
        server.cdn_origin_host = host.strip().lower()
        server.cdn_port = port
        # An unguessable path keeps scanners that find the origin port from
        # telling it apart from any other HTTPS server.
        server.cdn_path = path or server.cdn_path or f"/{secrets.token_urlsafe(9)}/"
        await session.commit()
        print(f"server {server.id} {server.name}: origin {server.cdn_origin_host}:{server.cdn_port}")
        print("Put these into the node's agent.env and restart agent + xray:")
        print(f"  XRAY_CDN_PORT={server.cdn_port}")
        print(f"  XRAY_CDN_PATH={server.cdn_path}")
        print(f"  XRAY_CDN_HOST={server.cdn_origin_host}")
        print(f"DNS: {server.cdn_origin_host} A {server.host} (DNS only, not proxied)")
        # XRAY_CDN_HOST makes REALITY pass this SNI on to the (loopback-only) CDN
        # inbound, so the CDN reaches the origin on 443 like any HTTPS site.
        print(f"CDN resource origin: https://{server.cdn_origin_host} (port 443)")


async def _add(args: argparse.Namespace) -> None:
    async with async_session_maker() as session:
        if await session.get(Server, args.server_id) is None:
            raise SystemExit(f"no server {args.server_id}")
        endpoint = CdnEndpoint(
            server_id=args.server_id,
            provider=args.provider,
            host=args.host.strip().lower(),
            address=args.address,
            sni=args.sni,
            xhttp_extra=json.loads(args.extra) if args.extra else None,
            position=args.position,
        )
        session.add(endpoint)
        await session.commit()
        print(f"added CDN endpoint #{endpoint.id}")


async def _set_enabled(endpoint_id: int, enabled: bool) -> None:
    async with async_session_maker() as session:
        endpoint = await session.get(CdnEndpoint, endpoint_id)
        if endpoint is None:
            raise SystemExit(f"no CDN endpoint {endpoint_id}")
        endpoint.enabled = enabled
        await session.commit()
        print(f"CDN endpoint #{endpoint_id} {'enabled' if enabled else 'disabled'}")


async def _remove(endpoint_id: int) -> None:
    async with async_session_maker() as session:
        endpoint = await session.get(CdnEndpoint, endpoint_id)
        if endpoint is None:
            raise SystemExit(f"no CDN endpoint {endpoint_id}")
        await session.delete(endpoint)
        await session.commit()
        print(f"removed CDN endpoint #{endpoint_id}")


async def _check() -> None:
    async with async_session_maker() as session:
        rows = (
            await session.execute(select(CdnEndpoint, Server).join(Server, Server.id == CdnEndpoint.server_id))
        ).all()
    for endpoint, server in rows:
        ok, detail = await cdn_service.probe_endpoint(server, endpoint)
        print(f"#{endpoint.id} {endpoint.provider} {endpoint.host}: {'OK' if ok else 'FAIL'} - {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.cdn_admin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("check")
    origin = commands.add_parser("origin", help="configure a node's CDN origin inbound")
    origin.add_argument("server_id", type=int)
    origin.add_argument("--host", required=True, help="origin hostname covered by the origin certificate")
    origin.add_argument("--port", type=int, default=8443)
    origin.add_argument("--path", help="inbound path (default: keep current or generate)")
    add = commands.add_parser("add", help="register a CDN resource proxying to a node")
    add.add_argument("server_id", type=int)
    add.add_argument("--provider", required=True)
    add.add_argument("--host", required=True, help="the CDN resource hostname")
    add.add_argument("--address", help="connect address if not the host (an edge IP)")
    add.add_argument("--sni", help="TLS SNI if not the host (domain fronting)")
    add.add_argument("--extra", help="xhttp extra as JSON")
    add.add_argument("--position", type=int, default=0)
    for name in ("enable", "disable", "remove"):
        command = commands.add_parser(name)
        command.add_argument("endpoint_id", type=int)
    args = parser.parse_args()

    async def run() -> None:
        if args.command == "list":
            await _list()
        elif args.command == "check":
            await _check()
        elif args.command == "origin":
            await _origin(args.server_id, args.host, args.port, args.path)
        elif args.command == "add":
            await _add(args)
        elif args.command in {"enable", "disable"}:
            await _set_enabled(args.endpoint_id, args.command == "enable")
        elif args.command == "remove":
            await _remove(args.endpoint_id)

    asyncio.run(run())


if __name__ == "__main__":
    main()
