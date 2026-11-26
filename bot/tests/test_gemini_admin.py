import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.control.state import build_desired_items
from src.core.database import async_session_maker, engine
from src.handlers.admin import gemini
from src.models import Server
from src.models.base import Base
from src.services.gemini_policy import set_policy, validate_policy

CAPS = {"features": ["gemini-policy-v1", "gemini-warp-v1", "cascade-v2"]}


async def seed(**overrides):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    fields = {
        "name": "Test",
        "flag": "T",
        "host": "203.0.113.1",
        "port": 443,
        "public_key": "keep",
        "short_id": "keep",
        "agent_url": "http://node",
        "agent_token": "keep",
        "control_mode": "pull",
        "control_capabilities": CAPS,
    }
    fields.update(overrides)
    async with async_session_maker() as s:
        node = Server(**fields)
        s.add(node)
        await s.commit()
        return node.id


async def test_policy_is_opt_in_published_and_acknowledged():
    node_id = await seed()
    async with async_session_maker() as s:
        assert not any(i["kind"] == "gemini_policy" for i in await build_desired_items(s, node_id))
        policy = {"mode": "warp", "ip_version": "ipv6"}
        node = await set_policy(s, node_id, policy, None)
        await s.commit()
        assert node.gemini_policy_generation == node.desired_generation > 0
        items = await build_desired_items(s, node_id)
        assert [i for i in items if i["kind"] == "gemini_policy"] == [{"kind": "gemini_policy", "policy": policy}]
        assert "Ожидается" in gemini.panel(node)[0]
        node.applied_generation = node.gemini_policy_generation
        assert "Применено агентом" in gemini.panel(node)[0]
        node.control_last_error = "error"
        assert "не подтверждено" in gemini.panel(node)[0]
        assert node.public_key == node.short_id == node.agent_token == "keep"
        with pytest.raises(ValueError, match="уже изменены"):
            await set_policy(s, node_id, {"mode": "warp", "ip_version": "ipv4"}, None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"control_mode": "push"},
        {"is_active": False},
        {"control_capabilities": {}},
        {"control_capabilities": {"features": ["gemini-policy-v1"]}},
    ],
)
async def test_apply_requires_node_support_and_warp(overrides):
    node_id = await seed(**overrides)
    async with async_session_maker() as s:
        with pytest.raises(ValueError):
            await set_policy(s, node_id, {"mode": "warp", "ip_version": "ipv4"}, None)
        await s.rollback()
        assert (await s.get(Server, node_id)).gemini_policy is None


async def test_older_agents_never_receive_new_item():
    node_id = await seed(control_capabilities={}, gemini_policy={"mode": "warp", "ip_version": "ipv4"})
    async with async_session_maker() as s:
        assert not any(i["kind"] == "gemini_policy" for i in await build_desired_items(s, node_id))


@pytest.mark.parametrize("url", ["https://dns.example.com/dns-query", "tls://dns.example.com:853"])
async def test_dns_mode_needs_no_warp(url):
    node_id = await seed(control_capabilities={"features": ["gemini-policy-v1"]})
    async with async_session_maker() as s:
        node = await set_policy(s, node_id, {"mode": "dns", "resolver": url}, None)
        assert node.gemini_policy["resolver"] == url


async def test_unauthorized_admin_callback_does_not_touch_state(monkeypatch):
    monkeypatch.setattr(gemini, "is_admin", lambda _: False)
    call = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock(), data="gemini:apply:1")
    state = SimpleNamespace(get_data=AsyncMock())
    await gemini.callback(call, state)
    state.get_data.assert_not_called()
    call.answer.assert_awaited_once_with("Доступ запрещён", show_alert=True)


def test_bot_and_agent_validation_contract_identical():
    root = Path(__file__).resolve().parents[2]

    def fn(path):
        return next(
            n
            for n in ast.parse(path.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == "validate_policy"
        )

    assert ast.dump(fn(root / "agent/app/gemini_policy.py")) == ast.dump(fn(root / "bot/src/services/gemini_policy.py"))


@pytest.mark.parametrize(
    "url",
    [
        "http://dns.example.com",
        "tls://127.0.0.1",
        "https://dns.example.com/?key=secret",
        "tls://dns.example.com/path",
        "https://user@dns.example.com/dns-query",
        "tls://[::1]",
    ],
)
def test_bad_dns_addresses_rejected(url):
    with pytest.raises(ValueError):
        validate_policy({"mode": "dns", "resolver": url})
