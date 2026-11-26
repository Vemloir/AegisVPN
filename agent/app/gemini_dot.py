"""Loopback DNS/TCP -> verified DNS/TLS for Xray (which has no native DoT)."""

import asyncio
import ssl
from contextlib import suppress
from urllib.parse import urlsplit

from . import gemini_policy

_server: asyncio.Server | None = None
_tasks: set[asyncio.Task] = set()
_LIMIT = 32


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    task = asyncio.current_task()
    if len(_tasks) >= _LIMIT:
        writer.close()
        await writer.wait_closed()
        return
    _tasks.add(task)
    upstream = None
    try:
        # One session is bounded; no plaintext or native-DNS fallback on errors.
        async with asyncio.timeout(15):
            policy = gemini_policy.load_policy()
            if not policy or policy["mode"] != "dns" or not policy["resolver"].startswith("tls://"):
                return
            parts = urlsplit(policy["resolver"])
            remote, upstream = await asyncio.open_connection(
                parts.hostname,
                parts.port or 853,
                ssl=ssl.create_default_context(),
                server_hostname=parts.hostname,
                ssl_handshake_timeout=5,
            )
            # Xray can reuse a TCP DNS connection for multiple framed queries.
            for _ in range(32):
                size = await reader.readexactly(2)
                length = int.from_bytes(size, "big")
                if not 12 <= length <= 65535:
                    return
                query = await reader.readexactly(length)
                if query[2] & 0x80:  # only DNS queries, never response injection
                    return
                upstream.write(size + query)
                await upstream.drain()
                size = await remote.readexactly(2)
                length = int.from_bytes(size, "big")
                if length < 12:
                    return
                answer = await remote.readexactly(length)
                if answer[:2] != query[:2] or not answer[2] & 0x80:
                    return
                writer.write(size + answer)
                await writer.drain()
    except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError):
        pass  # caller sees DNS failure; never send an unverified answer
    finally:
        if upstream is not None:
            upstream.close()
            with suppress(Exception):
                await upstream.wait_closed()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        _tasks.discard(task)


async def start() -> None:
    global _server
    if _server is None:
        _server = await asyncio.start_server(_serve, "127.0.0.1", gemini_policy.DOT_PORT)


async def stop() -> None:
    global _server
    if _server is not None:
        _server.close()
        await _server.wait_closed()
        _server = None
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
