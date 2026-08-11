"""Control hosts must not recreate VPN nodes from stale data-plane volumes."""

from sqlalchemy import select

from src.core.bootstrap import bootstrap_server
from src.core.config import settings
from src.core.database import async_session_maker, engine
from src.models import Server
from src.models.base import Base


async def test_server_bootstrap_is_opt_in(monkeypatch, tmp_path):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    stale_agent_env = tmp_path / "agent.env"
    stale_agent_env.write_text(
        "HOST_IP=89.125.181.236\n"
        "XRAY_PORT=443\n"
        "PUBLIC_KEY=stale-public-key\n"
        "SHORT_ID=stale-short-id\n"
        "AGENT_TOKEN=stale-agent-token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "bootstrap_server_agent_env", str(stale_agent_env))
    monkeypatch.setattr(settings, "bootstrap_server_name", "Finland | Helsinki")
    monkeypatch.setattr(settings, "bootstrap_server_flag", "🇫🇮")
    monkeypatch.setattr(settings, "bootstrap_server_agent_url", "http://127.0.0.1:8444")

    await bootstrap_server()

    async with async_session_maker() as session:
        servers = (await session.execute(select(Server))).scalars().all()
    assert servers == []
