"""Lightweight, idempotent schema migrations applied on startup.

The project deliberately does not use Alembic: the schema only ever grows by
nullable/defaulted columns, which both SQLite and PostgreSQL can add in place.
Each :class:`Column` is added only if it is missing, so this is safe to run on
every boot and against a database at any prior version.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import async_session_maker


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    sqlite_ddl: str
    pg_ddl: str
    post_sql: str | None = None  # extra statement run right after the column is added


# Ordered table -> columns that may be missing on older databases.
MIGRATIONS: dict[str, list[Column]] = {
    "users": [
        Column("language", "VARCHAR(8) DEFAULT 'ru'", "VARCHAR(8) DEFAULT 'ru'"),
        Column("trial_used", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        Column("privacy_accepted", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        Column("conn_limit", "INTEGER", "INTEGER"),
        # Legal-acceptance gate (Privacy Policy + Terms of Service). The grandfather
        # backfill that silently re-accepted old privacy-only users has been removed:
        # the policy is now "explicit acceptance required". The one-shot reset in
        # ONE_SHOT_DATA_MIGRATIONS forces EVERY user (including those the original
        # grandfather migration already marked accepted in prod) to re-accept.
        Column("accepted_terms_at", "TIMESTAMP", "TIMESTAMP"),
        Column("accepted_terms_version", "VARCHAR(32)", "VARCHAR(32)"),
        # Profile name and avatar from the Telegram Login Widget payload, all
        # refreshed on every web sign-in (Telegram's CDN links change when the
        # user changes photo, and names change freely).
        Column("first_name", "VARCHAR(128)", "VARCHAR(128)"),
        Column("last_name", "VARCHAR(128)", "VARCHAR(128)"),
        Column("photo_url", "VARCHAR(512)", "VARCHAR(512)"),
    ],
    "plans": [
        Column("rub_price", "INTEGER", "INTEGER"),
        # The reference plan. Existing installs get the 30-day plan marked, if
        # they have exactly one — the conventional "a month" against which the
        # other terms are compared. Where that is ambiguous, nothing is marked
        # and the site simply falls back to the first plan until an admin picks.
        Column(
            "is_base",
            "BOOLEAN DEFAULT 0",
            "BOOLEAN DEFAULT FALSE",
            post_sql=(
                "UPDATE plans SET is_base = 1 WHERE id = ("
                "SELECT id FROM plans WHERE days = 30 AND is_active = 1 "
                "ORDER BY id LIMIT 1) AND NOT EXISTS (SELECT 1 FROM plans WHERE is_base = 1)"
            ),
        ),
    ],
    "payments": [
        Column("provider", "VARCHAR(16) DEFAULT 'stars'", "VARCHAR(16) DEFAULT 'stars'"),
        Column("rub_amount", "INTEGER", "INTEGER"),
        Column("status", "VARCHAR(16)", "VARCHAR(16)"),
    ],
    "subscriptions": [
        Column("legacy_sub_token", "VARCHAR(255)", "VARCHAR(255)"),
        Column("traffic_up_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_down_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
    ],
    "subscription_servers": [
        Column("traffic_last_up", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_last_down", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_up_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_down_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_cursors", "JSON", "JSONB"),
    ],
    "devices": [
        Column("is_suspended", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        Column("last_server_id", "INTEGER", "INTEGER"),
        Column("traffic_up_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_down_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("os_label", "VARCHAR(64)", "VARCHAR(64)"),
        Column("build_number", "VARCHAR(32)", "VARCHAR(32)"),
        Column("added_location", "VARCHAR(128)", "VARCHAR(128)"),
        Column("added_country_code", "VARCHAR(2)", "VARCHAR(2)"),
    ],
    "servers": [
        Column(
            "subscription_group",
            "VARCHAR(16) DEFAULT 'safe'",
            "VARCHAR(16) DEFAULT 'safe'",
            post_sql="UPDATE servers SET subscription_group = 'safe' WHERE subscription_group IS NULL",
        ),
        Column("display_order", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
        # Lifetime per-location traffic, kept on the server row so it outlives the
        # SubscriptionServer links (which reconcile deletes on disable/re-sync,
        # taking their per-location bytes with them). Seeded once from whatever the
        # surviving links still hold — the only per-location figure the DB retains;
        # traffic on already-deleted links is unrecoverable, so pre-existing
        # locations start from an undercount and are exact from here on.
        Column(
            "traffic_up_bytes",
            "BIGINT DEFAULT 0",
            "BIGINT DEFAULT 0",
            post_sql=(
                "UPDATE servers SET traffic_up_bytes = COALESCE("
                "(SELECT SUM(traffic_up_bytes) FROM subscription_servers "
                "WHERE subscription_servers.server_id = servers.id), 0)"
            ),
        ),
        Column(
            "traffic_down_bytes",
            "BIGINT DEFAULT 0",
            "BIGINT DEFAULT 0",
            post_sql=(
                "UPDATE servers SET traffic_down_bytes = COALESCE("
                "(SELECT SUM(traffic_down_bytes) FROM subscription_servers "
                "WHERE subscription_servers.server_id = servers.id), 0)"
            ),
        ),
        # The website's only presentation field: an ISO 3166-1 alpha-2 code. The
        # globe derives the country outline and its camera target from the atlas,
        # and the region filter is a function of the code — storing either here
        # would be a second copy of something already known. Operator-set; a node
        # without it is still served, just not drawn on the globe.
        Column("country_code", "VARCHAR(2)", "VARCHAR(2)"),
        Column("mtproxy_secret", "VARCHAR(64)", "VARCHAR(64)"),
        # MTProto-proxy listen port. No post_sql: operator-set with the secret.
        Column("mtproxy_port", "INTEGER", "INTEGER"),
        # Alternative VLESS+REALITY transport port (same reality keypair). NULL =
        # xhttp/443 only, no transport choice. Operator-set per node.
        Column("tcp_port", "INTEGER", "INTEGER"),
        # --- Hysteria2 capability. Every field here is operator-set via a direct
        # DB UPDATE, never backfilled from the migration: the obfs password and
        # the SNI are secrets, and until they are present hy2_capable is False and
        # the bot falls back to vless rather than shipping a broken Hy2 link.
        Column("hy2_enabled", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        Column("hy2_port", "INTEGER", "INTEGER"),
        Column("hy2_hop_start", "INTEGER", "INTEGER"),
        Column("hy2_hop_end", "INTEGER", "INTEGER"),
        Column("hy2_obfs_password", "VARCHAR(255)", "VARCHAR(255)"),
        Column("hy2_up", "VARCHAR(32)", "VARCHAR(32)"),
        Column("hy2_down", "VARCHAR(32)", "VARCHAR(32)"),
        # The Hy2 TLS SNI (the shared CA/Let's Encrypt cert domain) is set
        # out-of-band by the operator via a direct DB update, like the obfs
        # password — never the actual domain in the (public) migration.
        Column("hy2_sni", "VARCHAR(255)", "VARCHAR(255)"),
    ],
}


# One-shot data migrations: run exactly once per database, tracked in the
# ``schema_meta`` marker table. Unlike Column.post_sql (which fires only when its
# column is first added), these run on the FIRST boot that sees them and never
# again — so a force-reset does not re-trigger on every restart and re-gate users
# who just re-accepted. Ordered; each runs after all column adds above.
ONE_SHOT_DATA_MIGRATIONS: list[tuple[str, str]] = [
    # Force EVERY existing user to re-accept the current ToS + Privacy. The
    # earlier grandfather migration already ran in prod and marked the existing
    # users accepted; this clears that so they are re-gated on next interaction.
    (
        "force_terms_reacceptance_2026_06_22",
        "UPDATE users SET accepted_terms_version = NULL, accepted_terms_at = NULL, "
        "privacy_accepted = 0",
    ),
]


# Ordered table -> columns to drop from older databases (legacy/removed features).
# Dropped only if present, so this is idempotent. SQLite (>=3.35) and PostgreSQL
# both support ALTER TABLE ... DROP COLUMN.
DROP_COLUMNS: dict[str, list[str]] = {
    "servers": ["static_uri"],
}


def _is_sqlite() -> bool:
    return settings.db_url.startswith("sqlite+aiosqlite")


async def _existing_columns(session: AsyncSession, table: str, is_sqlite: bool) -> set[str]:
    if is_sqlite:
        result = await session.execute(text(f"PRAGMA table_info({table})"))
        return {row[1] for row in result.fetchall()}
    result = await session.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
        {"table": table},
    )
    return {row[0] for row in result.fetchall()}


async def run_migrations() -> None:
    """Add any columns missing from the live schema. Idempotent."""
    # Ensure all model-defined tables exist (create_all is safe; it never drops or modifies).
    from src.core.database import engine
    from src.models import Base  # noqa: F401 — triggers all model imports

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    is_sqlite = _is_sqlite()
    async with async_session_maker() as session:
        for table, columns in MIGRATIONS.items():
            existing = await _existing_columns(session, table, is_sqlite)
            for column in columns:
                if column.name in existing:
                    continue
                ddl = column.sqlite_ddl if is_sqlite else column.pg_ddl
                await session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column.name} {ddl}"))
                if column.post_sql:
                    await session.execute(text(column.post_sql))
        for table, drop_names in DROP_COLUMNS.items():
            existing = await _existing_columns(session, table, is_sqlite)
            for name in drop_names:
                if name not in existing:
                    continue
                await session.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))

        # One-shot data migrations run AFTER every column add/drop above, each
        # exactly once, tracked in schema_meta. This is what actually forces the
        # terms re-acceptance on deploy.
        await session.execute(
            text("CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR(128) PRIMARY KEY)")
        )
        applied = {
            row[0]
            for row in (await session.execute(text("SELECT key FROM schema_meta"))).fetchall()
        }
        for key, sql in ONE_SHOT_DATA_MIGRATIONS:
            if key in applied:
                continue
            await session.execute(text(sql))
            await session.execute(text("INSERT INTO schema_meta (key) VALUES (:k)"), {"k": key})
        await session.commit()
