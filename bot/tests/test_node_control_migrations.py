from sqlalchemy import text

from src.core.database import async_session_maker, engine
from src.core.migrations import run_migrations
from src.models import Base


async def _column_names(table: str) -> set[str]:
    async with async_session_maker() as session:
        result = await session.execute(text(f"PRAGMA table_info({table})"))
        return {row[1] for row in result.fetchall()}


async def _table_names() -> set[str]:
    async with async_session_maker() as session:
        result = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
        return {row[0] for row in result.fetchall()}


async def test_control_schema_is_created_idempotently():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    await run_migrations()
    await run_migrations()

    assert {
        "control_mode",
        "control_token_hash",
        "control_cert_fingerprint",
        "control_previous_token_hash",
        "control_previous_cert_fingerprint",
        "control_previous_credential_expires_at",
        "desired_generation",
        "applied_generation",
        "applied_digest",
        "control_last_seen_at",
        "control_last_reconciled_at",
        "control_last_error",
        "control_agent_version",
        "control_capabilities",
        "node_role",
    } <= await _column_names("servers")
    assert {
        "node_snapshots",
        "node_snapshot_pages",
        "node_telemetry",
        "cascade_routes",
        "cascade_route_exits",
        "cascade_route_acks",
    } <= await _table_names()
    assert "schema_version" in await _column_names("node_snapshots")
    assert "schema_version" in await _column_names("node_snapshot_pages")
