import asyncio
import json
from pathlib import Path

from sqlalchemy import select, text

from src.core.config import settings
from src.core.database import async_session_maker, init_db
from src.core.logger import logger
from src.models import Plan, Server


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


async def wait_for_agent_env(path: Path) -> bool:
    if path.exists():
        return True

    for _ in range(settings.bootstrap_server_wait_seconds):
        await asyncio.sleep(1)
        if path.exists():
            return True

    return False


async def bootstrap_plans() -> None:
    if not settings.bootstrap_plans_json:
        return

    try:
        plans_data = json.loads(settings.bootstrap_plans_json)
    except json.JSONDecodeError as exc:
        logger.error(f"Failed to parse BOOTSTRAP_PLANS_JSON: {exc}")
        return

    if not isinstance(plans_data, list):
        logger.error("BOOTSTRAP_PLANS_JSON must be a JSON list")
        return

    async with async_session_maker() as session:
        changed = False

        for item in plans_data:
            if not isinstance(item, dict):
                continue

            days = int(item.get("days", 0))
            stars_price = int(item.get("stars_price", 0))
            is_active = bool(item.get("is_active", True))

            if days <= 0 or stars_price <= 0:
                continue

            result = await session.execute(select(Plan).where(Plan.days == days))
            plan = result.scalar_one_or_none()

            if plan is None:
                session.add(Plan(days=days, stars_price=stars_price, is_active=is_active))
                changed = True
                continue

            if plan.stars_price != stars_price or plan.is_active != is_active:
                plan.stars_price = stars_price
                plan.is_active = is_active
                changed = True

        if changed:
            await session.commit()


async def ensure_user_columns() -> None:
    async with async_session_maker() as session:
        if settings.db_url.startswith("sqlite+aiosqlite"):
            result = await session.execute(text("PRAGMA table_info(users)"))
            columns = {row[1] for row in result.fetchall()}
            if "language" not in columns:
                await session.execute(text("ALTER TABLE users ADD COLUMN language VARCHAR(8) DEFAULT 'ru'"))
            if "trial_used" not in columns:
                await session.execute(text("ALTER TABLE users ADD COLUMN trial_used BOOLEAN DEFAULT 0"))
            if "privacy_accepted" not in columns:
                await session.execute(text("ALTER TABLE users ADD COLUMN privacy_accepted BOOLEAN DEFAULT 0"))
            await session.commit()
            return

        result = await session.execute(
            text(
                """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'users'
            """
            )
        )
        columns = {row[0] for row in result.fetchall()}
        if "language" not in columns:
            await session.execute(text("ALTER TABLE users ADD COLUMN language VARCHAR(8) DEFAULT 'ru'"))
        if "trial_used" not in columns:
            await session.execute(text("ALTER TABLE users ADD COLUMN trial_used BOOLEAN DEFAULT FALSE"))
        if "privacy_accepted" not in columns:
            await session.execute(text("ALTER TABLE users ADD COLUMN privacy_accepted BOOLEAN DEFAULT FALSE"))
        await session.commit()


async def ensure_subscription_columns() -> None:
    async with async_session_maker() as session:
        if settings.db_url.startswith("sqlite+aiosqlite"):
            result = await session.execute(text("PRAGMA table_info(subscriptions)"))
            columns = {row[1] for row in result.fetchall()}
            if "legacy_sub_token" not in columns:
                await session.execute(text("ALTER TABLE subscriptions ADD COLUMN legacy_sub_token VARCHAR(255)"))
            if "amnezia_private_key" not in columns:
                await session.execute(text("ALTER TABLE subscriptions ADD COLUMN amnezia_private_key VARCHAR(255)"))
            if "amnezia_public_key" not in columns:
                await session.execute(text("ALTER TABLE subscriptions ADD COLUMN amnezia_public_key VARCHAR(255)"))
            if "amnezia_ipv4" not in columns:
                await session.execute(text("ALTER TABLE subscriptions ADD COLUMN amnezia_ipv4 VARCHAR(64)"))
            if "traffic_up_bytes" not in columns:
                await session.execute(text("ALTER TABLE subscriptions ADD COLUMN traffic_up_bytes BIGINT DEFAULT 0"))
            if "traffic_down_bytes" not in columns:
                await session.execute(text("ALTER TABLE subscriptions ADD COLUMN traffic_down_bytes BIGINT DEFAULT 0"))
            await session.commit()
            return

        result = await session.execute(
            text(
                """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'subscriptions'
            """
            )
        )
        columns = {row[0] for row in result.fetchall()}
        if "legacy_sub_token" not in columns:
            await session.execute(text("ALTER TABLE subscriptions ADD COLUMN legacy_sub_token VARCHAR(255)"))
        if "amnezia_private_key" not in columns:
            await session.execute(text("ALTER TABLE subscriptions ADD COLUMN amnezia_private_key VARCHAR(255)"))
        if "amnezia_public_key" not in columns:
            await session.execute(text("ALTER TABLE subscriptions ADD COLUMN amnezia_public_key VARCHAR(255)"))
        if "amnezia_ipv4" not in columns:
            await session.execute(text("ALTER TABLE subscriptions ADD COLUMN amnezia_ipv4 VARCHAR(64)"))
        if "traffic_up_bytes" not in columns:
            await session.execute(text("ALTER TABLE subscriptions ADD COLUMN traffic_up_bytes BIGINT DEFAULT 0"))
        if "traffic_down_bytes" not in columns:
            await session.execute(text("ALTER TABLE subscriptions ADD COLUMN traffic_down_bytes BIGINT DEFAULT 0"))
        await session.commit()


async def ensure_subscription_server_columns() -> None:
    async with async_session_maker() as session:
        if settings.db_url.startswith("sqlite+aiosqlite"):
            result = await session.execute(text("PRAGMA table_info(subscription_servers)"))
            columns = {row[1] for row in result.fetchall()}
            if "amnezia_private_key" not in columns:
                await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN amnezia_private_key VARCHAR(255)"))
            if "amnezia_public_key" not in columns:
                await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN amnezia_public_key VARCHAR(255)"))
            if "amnezia_ipv4" not in columns:
                await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN amnezia_ipv4 VARCHAR(64)"))
            if "traffic_last_up" not in columns:
                await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN traffic_last_up BIGINT DEFAULT 0"))
            if "traffic_last_down" not in columns:
                await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN traffic_last_down BIGINT DEFAULT 0"))
            if "traffic_up_bytes" not in columns:
                await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN traffic_up_bytes BIGINT DEFAULT 0"))
            if "traffic_down_bytes" not in columns:
                await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN traffic_down_bytes BIGINT DEFAULT 0"))
            await session.commit()
            return

        result = await session.execute(
            text(
                """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'subscription_servers'
            """
            )
        )
        columns = {row[0] for row in result.fetchall()}
        if "amnezia_private_key" not in columns:
            await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN amnezia_private_key VARCHAR(255)"))
        if "amnezia_public_key" not in columns:
            await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN amnezia_public_key VARCHAR(255)"))
        if "amnezia_ipv4" not in columns:
            await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN amnezia_ipv4 VARCHAR(64)"))
        if "traffic_last_up" not in columns:
            await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN traffic_last_up BIGINT DEFAULT 0"))
        if "traffic_last_down" not in columns:
            await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN traffic_last_down BIGINT DEFAULT 0"))
        if "traffic_up_bytes" not in columns:
            await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN traffic_up_bytes BIGINT DEFAULT 0"))
        if "traffic_down_bytes" not in columns:
            await session.execute(text("ALTER TABLE subscription_servers ADD COLUMN traffic_down_bytes BIGINT DEFAULT 0"))
        await session.commit()


async def ensure_server_columns() -> None:
    async with async_session_maker() as session:
        if settings.db_url.startswith("sqlite+aiosqlite"):
            result = await session.execute(text("PRAGMA table_info(servers)"))
            columns = {row[1] for row in result.fetchall()}
            if "subscription_group" not in columns:
                await session.execute(
                    text("ALTER TABLE servers ADD COLUMN subscription_group VARCHAR(16) DEFAULT 'safe'")
                )
                await session.execute(
                    text("UPDATE servers SET subscription_group = 'safe' WHERE subscription_group IS NULL")
                )
            if "amnezia_enabled" not in columns:
                await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_enabled BOOLEAN DEFAULT 0"))
            if "amnezia_name" not in columns:
                await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_name VARCHAR(255)"))
            if "amnezia_endpoint_host" not in columns:
                await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_endpoint_host VARCHAR(255)"))
            if "amnezia_port" not in columns:
                await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_port INTEGER"))
            if "amnezia_public_key" not in columns:
                await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_public_key VARCHAR(255)"))
            await session.commit()
            return

        result = await session.execute(
            text(
                """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'servers'
            """
            )
        )
        columns = {row[0] for row in result.fetchall()}
        if "subscription_group" not in columns:
            await session.execute(
                text("ALTER TABLE servers ADD COLUMN subscription_group VARCHAR(16) DEFAULT 'safe'")
            )
            await session.execute(
                text("UPDATE servers SET subscription_group = 'safe' WHERE subscription_group IS NULL")
            )
        if "amnezia_enabled" not in columns:
            await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_enabled BOOLEAN DEFAULT FALSE"))
        if "amnezia_name" not in columns:
            await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_name VARCHAR(255)"))
        if "amnezia_endpoint_host" not in columns:
            await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_endpoint_host VARCHAR(255)"))
        if "amnezia_port" not in columns:
            await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_port INTEGER"))
        if "amnezia_public_key" not in columns:
            await session.execute(text("ALTER TABLE servers ADD COLUMN amnezia_public_key VARCHAR(255)"))
        await session.commit()


async def ensure_default_plan_exists() -> None:
    async with async_session_maker() as session:
        existing = (await session.execute(select(Plan).where(Plan.is_active == True))).scalars().first()
        if existing is not None:
            return

        any_plan = (await session.execute(select(Plan).where(Plan.days == 30))).scalar_one_or_none()
        if any_plan is not None:
            any_plan.is_active = True
        else:
            session.add(Plan(days=30, stars_price=100, is_active=True))
        await session.commit()


async def bootstrap_server() -> None:
    agent_env_path = Path(settings.bootstrap_server_agent_env)
    if not await wait_for_agent_env(agent_env_path):
        logger.warning(f"Agent env file not found: {agent_env_path}")
        return

    agent_env = read_env_file(agent_env_path)
    required_keys = ["HOST_IP", "XRAY_PORT", "PUBLIC_KEY", "SHORT_ID", "AGENT_TOKEN"]
    if not all(agent_env.get(key) for key in required_keys):
        logger.warning("Agent env file is missing required values for server bootstrap")
        return

    async with async_session_maker() as session:
        result = await session.execute(
            select(Server).where(
                Server.agent_url == settings.bootstrap_server_agent_url,
                Server.name == settings.bootstrap_server_name,
            )
        )
        server = result.scalar_one_or_none()

        if server is None:
            fallback = await session.execute(
                select(Server)
                .where(Server.agent_url == settings.bootstrap_server_agent_url)
                .order_by(Server.id)
            )
            server = fallback.scalars().first()

        values = {
            "name": settings.bootstrap_server_name,
            "flag": settings.bootstrap_server_flag,
            "host": agent_env["HOST_IP"],
            "port": int(agent_env["XRAY_PORT"]),
            "public_key": agent_env["PUBLIC_KEY"],
            "short_id": agent_env["SHORT_ID"],
            "agent_url": settings.bootstrap_server_agent_url,
            "agent_token": agent_env["AGENT_TOKEN"],
            "amnezia_enabled": bool(
                settings.amnezia_enabled
                and settings.amnezia_server_host
                and settings.amnezia_server_public_key
            ),
            "amnezia_name": f"{settings.bootstrap_server_name} Amnezia",
            "amnezia_endpoint_host": settings.amnezia_server_host,
            "amnezia_port": settings.amnezia_server_port if settings.amnezia_enabled else None,
            "amnezia_public_key": settings.amnezia_server_public_key,
            "subscription_group": settings.bootstrap_server_subscription_group,
            "is_active": True,
        }

        if server is None:
            session.add(Server(access_mode="public", **values))
        else:
            for key, value in values.items():
                setattr(server, key, value)

        await session.commit()


async def bootstrap_application() -> None:
    if settings.auto_init_db:
        await init_db()

    await ensure_user_columns()
    await ensure_subscription_columns()
    await ensure_subscription_server_columns()
    await ensure_server_columns()
    await bootstrap_plans()
    await ensure_default_plan_exists()
    await bootstrap_server()
