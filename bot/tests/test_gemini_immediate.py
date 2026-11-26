from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.control.state import build_desired_items
from src.core.config import settings
from src.core.database import async_session_maker
from src.handlers.admin import gemini
from src.models import Device, Server, Subscription, SubscriptionServer, User
from src.services.gemini_policy import DECODO_IP_CAPABILITY, set_admin_proxy, set_policy
from tests.test_gemini_admin import CAPS, seed


class State:
    def __init__(self):
        self.data = {}
        self.value = None

    async def clear(self):
        self.data = {}
        self.value = None

    async def set_state(self, value):
        self.value = value.state

    async def get_state(self):
        return self.value

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)


def call(node_id, action):
    return SimpleNamespace(
        data=f"gemini:{action}:{node_id}",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_controls_save_immediately_and_preserve_other_mode(monkeypatch):
    monkeypatch.setattr(gemini, "is_admin", lambda _: True)
    node_id = await seed(gemini_policy={"mode": "warp", "ip_version": "ipv6"})
    state = State()
    await gemini.callback(call(node_id, "open"), state)
    await gemini.callback(call(node_id, "ipv4"), state)
    async with async_session_maker() as session:
        node = await session.get(Server, node_id)
        assert node.gemini_policy == {"mode": "warp", "ip_version": "ipv4"}
        labels = [b.text for row in gemini.panel(node)[1].inline_keyboard for b in row]
        assert labels[:2] == ["Доступ: WARP", "Тип IP: IPv4"]
        assert not any("Применить" in label or "Черновик" in label for label in labels)
    await gemini.callback(call(node_id, "dns"), state)
    # Incomplete DNS choice has no side effects until a valid address is sent.
    async with async_session_maker() as session:
        assert (await session.get(Server, node_id)).gemini_policy["mode"] == "warp"
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123), text="https://dns.example.com/dns-query", answer=AsyncMock()
    )
    await gemini.resolver_input(message, state)
    async with async_session_maker() as session:
        assert (await session.get(Server, node_id)).gemini_policy["mode"] == "dns"
    await gemini.callback(call(node_id, "warp"), state)
    await gemini.callback(call(node_id, "dns"), state)
    async with async_session_maker() as session:
        node = await session.get(Server, node_id)
        assert node.gemini_policy["resolver"] == message.text
        assert node.gemini_preferences["warp"]["ip_version"] == "ipv4"


async def test_old_apply_button_does_nothing(monkeypatch):
    monkeypatch.setattr(gemini, "is_admin", lambda _: True)
    node_id = await seed()
    state = State()
    await gemini.callback(call(node_id, "open"), state)
    await gemini.callback(call(node_id, "apply"), state)
    async with async_session_maker() as session:
        assert (await session.get(Server, node_id)).gemini_policy is None


async def test_decodo_scopes_only_active_admin_identities(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [123])
    node_id = await seed(
        control_capabilities={"features": [*CAPS["features"], "gemini-admin-proxy-v1", DECODO_IP_CAPABILITY]},
        gemini_policy={"mode": "warp", "ip_version": "ipv6"},
    )
    async with async_session_maker() as session:
        admin = User(tg_id=123)
        other = User(tg_id=456)
        session.add_all([admin, other])
        await session.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        subs = []
        for i, user in enumerate([admin, other]):
            sub = Subscription(
                user_id=user.id,
                sub_token=f"token{i}",
                client_uuid=f"uuid{i}",
                plan_days=30,
                expires_at=now + timedelta(days=30),
                is_active=True,
            )
            session.add(sub)
            await session.flush()
            subs.append(sub)
            session.add(SubscriptionServer(subscription_id=sub.id, server_id=node_id, is_synced=True))
            session.add(Device(subscription_id=sub.id, uuid=f"device{i}", ua_fingerprint=f"ua{i}", display_name="test"))
        session.add(
            Device(
                subscription_id=subs[0].id, uuid="suspended", ua_fingerprint="x", display_name="x", is_suspended=True
            )
        )
        await session.commit()
        policy = {"mode": "warp", "ip_version": "ipv6"}
        node = await set_admin_proxy(session, node_id, policy, False)
        await session.commit()
        assert node.gemini_policy == policy  # never replaces global Gemini route
        items = await build_desired_items(session, node_id)
        item = next(i for i in items if i["kind"] == "gemini_admin_proxy")
        assert "port" not in item  # preserve legacy digest and default endpoint
        users = item["users"]
        assert len(users) == 2
        assert all(u.startswith(f"user_{admin.id}_sub_{subs[0].id}") for u in users)
        await set_admin_proxy(session, node_id, policy, True, port=10010, expected_port=10001)
        await session.commit()
        items = await build_desired_items(session, node_id)
        assert next(i for i in items if i["kind"] == "gemini_admin_proxy") == {**item, "port": 10010}
        with pytest.raises(ValueError, match="уже изменены"):
            await set_admin_proxy(session, node_id, policy, True, port=10002, expected_port=10001)
        await set_policy(session, node_id, {"mode": "warp", "ip_version": "ipv4"}, policy, expected_admin_proxy=True)
        await session.commit()
        items = await build_desired_items(session, node_id)
        assert next(i["users"] for i in items if i["kind"] == "gemini_admin_proxy") == []


async def test_pick_decodo_ip_immediately_and_no_changes_from_opening_menu(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [123])
    monkeypatch.setattr(gemini, "is_admin", lambda _: True)
    node_id = await seed(
        control_capabilities={"features": [*CAPS["features"], "gemini-admin-proxy-v1", DECODO_IP_CAPABILITY]}
    )
    state = State()
    await gemini.callback(call(node_id, "open"), state)
    opened = call(node_id, "decodo")
    await gemini.callback(opened, state)
    rows = opened.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert len([b for row in rows for b in row if b.callback_data.startswith("gemini:dc")]) == 10
    async with async_session_maker() as session:
        assert not (await session.get(Server, node_id)).gemini_admin_proxy
    await gemini.callback(call(node_id, "dc10007"), state)
    async with async_session_maker() as session:
        node = await session.get(Server, node_id)
        assert node.gemini_admin_proxy and node.gemini_preferences["decodo_port"] == 10007
        assert node.gemini_policy is None


async def test_old_agent_rejects_port_override(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [123])
    node_id = await seed(control_capabilities={"features": [*CAPS["features"], "gemini-admin-proxy-v1"]})
    async with async_session_maker() as session:
        with pytest.raises(ValueError, match="IP Decodo недоступен"):
            await set_admin_proxy(session, node_id, None, False, port=10002)


async def test_decodo_refuses_multiple_admins(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [123, 456])
    node_id = await seed(control_capabilities={"features": [*CAPS["features"], "gemini-admin-proxy-v1"]})
    async with async_session_maker() as session:
        with pytest.raises(ValueError, match="не подготовлен"):
            await set_admin_proxy(session, node_id, None, False)
