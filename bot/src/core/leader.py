from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text

from src.core.config import settings
from src.core.database import engine


@dataclass(frozen=True, slots=True)
class LeaderLease:
    name: str
    acquired: bool


def advisory_lock_key(name: str) -> int:
    """Stable signed bigint key accepted by PostgreSQL advisory locks."""
    digest = hashlib.blake2b(
        f"aegisvpn:{name}".encode(),
        digest_size=8,
        person=b"aegis-ha",
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


@asynccontextmanager
async def leader_lease(
    name: str,
    *,
    database_url: str | None = None,
    engine_override=None,
) -> AsyncIterator[LeaderLease]:
    url = database_url or settings.db_url
    # SQLite remains the deliberately single-instance development/rollback
    # mode. There is no cross-host lease to take there.
    if url.startswith("sqlite+"):
        yield LeaderLease(name=name, acquired=True)
        return

    key = advisory_lock_key(name)
    selected_engine = engine_override or engine
    async with selected_engine.connect() as connection:
        acquired = bool(
            (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": key},
                )
            ).scalar_one()
        )
        try:
            yield LeaderLease(name=name, acquired=acquired)
        finally:
            if acquired:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": key},
                )


async def run_leader_worker(
    name: str,
    worker: Callable[[], Awaitable[None]],
    *,
    lease_factory=leader_lease,
    retry_seconds: float = 5.0,
) -> None:
    """Run a long-lived worker only while this process owns its singleton lease.

    A standby retries until the active process releases its PostgreSQL advisory
    lock. Development SQLite remains single-instance and runs immediately.
    """
    while True:
        async with lease_factory(name) as lease:
            if lease.acquired:
                await worker()
                return
        await asyncio.sleep(retry_seconds)
