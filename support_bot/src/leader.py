from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import asyncpg


def advisory_lock_key(name: str) -> int:
    digest = hashlib.blake2b(
        f"aegisvpn:{name}".encode(),
        digest_size=8,
        person=b"aegis-ha",
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


@asynccontextmanager
async def support_leader_lease(database_url: str | None, name: str):
    if not database_url:
        yield True
        return

    # asyncpg accepts postgres:// / postgresql://, whereas the main application
    # URL may include SQLAlchemy's +asyncpg driver marker.
    url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(url)
    key = advisory_lock_key(name)
    acquired = bool(await connection.fetchval("SELECT pg_try_advisory_lock($1)", key))
    try:
        yield acquired
    finally:
        if acquired:
            await connection.fetchval("SELECT pg_advisory_unlock($1)", key)
        await connection.close()


async def run_support_leader(
    database_url: str | None,
    worker: Callable[[], Awaitable[None]],
    *,
    retry_seconds: float = 5.0,
    lease_factory=support_leader_lease,
) -> None:
    while True:
        async with lease_factory(database_url, "telegram-support-polling") as acquired:
            if acquired:
                await worker()
                return
        await asyncio.sleep(retry_seconds)
