from contextlib import asynccontextmanager

from src.core.leader import advisory_lock_key, leader_lease, run_leader_worker
from src.scheduler import singleton_job


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class Connection:
    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.statements = []

    async def execute(self, statement, params):
        self.statements.append((str(statement), params))
        if "pg_try_advisory_lock" in str(statement):
            return Result(self.acquired)
        return Result(True)


class Begin:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class Engine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return Begin(self.connection)


async def test_postgres_lease_uses_stable_signed_64_bit_advisory_lock():
    connection = Connection(True)
    async with leader_lease(
        "scheduler:traffic",
        database_url="postgresql+asyncpg://db/aegis",
        engine_override=Engine(connection),
    ) as lease:
        assert lease.acquired is True

    assert connection.statements[0][0].startswith("SELECT pg_try_advisory_lock")
    assert connection.statements[-1][0].startswith("SELECT pg_advisory_unlock")
    assert connection.statements[0][1]["key"] == advisory_lock_key("scheduler:traffic")
    assert -(2**63) <= connection.statements[0][1]["key"] < 2**63


async def test_singleton_job_suppresses_duplicate_scheduler_execution():
    calls = []

    @asynccontextmanager
    async def denied(_name):
        yield type("Lease", (), {"acquired": False})()

    wrapped = singleton_job("traffic", lambda: calls.append("ran"), lease_factory=denied)
    await wrapped()
    assert calls == []

    @asynccontextmanager
    async def granted(_name):
        yield type("Lease", (), {"acquired": True})()

    wrapped = singleton_job("traffic", lambda: calls.append("ran"), lease_factory=granted)
    await wrapped()
    assert calls == ["ran"]


async def test_leader_worker_retries_standby_then_runs_once():
    attempts = []
    calls = []

    @asynccontextmanager
    async def lease_factory(_name):
        attempts.append("lease")
        yield type("Lease", (), {"acquired": len(attempts) > 1})()

    async def worker():
        calls.append("ran")

    await run_leader_worker(
        "telegram-main-polling",
        worker,
        lease_factory=lease_factory,
        retry_seconds=0,
    )

    assert attempts == ["lease", "lease"]
    assert calls == ["ran"]
