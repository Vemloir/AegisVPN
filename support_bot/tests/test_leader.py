from contextlib import asynccontextmanager

from src.leader import advisory_lock_key, run_support_leader


async def test_support_polling_waits_for_database_leader_lease():
    attempts = []
    calls = []

    @asynccontextmanager
    async def lease_factory(database_url, name):
        attempts.append((database_url, name))
        yield len(attempts) > 1

    async def worker():
        calls.append("ran")

    await run_support_leader(
        "postgresql://db/aegis",
        worker,
        retry_seconds=0,
        lease_factory=lease_factory,
    )

    assert attempts == [
        ("postgresql://db/aegis", "telegram-support-polling"),
        ("postgresql://db/aegis", "telegram-support-polling"),
    ]
    assert calls == ["ran"]
    assert -(2**63) <= advisory_lock_key("telegram-support-polling") < 2**63
