from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

engine_kwargs = {
    "echo": settings.db_echo,
    "future": True,
}

if not settings.db_url.startswith("sqlite+aiosqlite"):
    engine_kwargs.update(
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=300,
    )

engine = create_async_engine(settings.db_url, **engine_kwargs)

if settings.db_url.startswith("sqlite+aiosqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with async_session_maker() as session:
        yield session


async def init_db() -> None:
    from src.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
