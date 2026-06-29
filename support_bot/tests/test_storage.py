import pytest

from src.storage import Storage


@pytest.fixture
async def store(tmp_path):
    s = Storage(str(tmp_path / "support.db"))
    await s.init()
    return s


async def test_roundtrip(store):
    await store.put(operator_chat_id=111, operator_msg_id=5, user_id=999)
    assert await store.get_user(111, 5) == 999


async def test_missing_returns_none(store):
    assert await store.get_user(111, 404) is None


async def test_same_msg_id_distinct_per_operator(store):
    # message_id space is per-chat, so the same id under two operators maps independently
    await store.put(operator_chat_id=111, operator_msg_id=5, user_id=999)
    await store.put(operator_chat_id=222, operator_msg_id=5, user_id=888)
    assert await store.get_user(111, 5) == 999
    assert await store.get_user(222, 5) == 888


async def test_put_is_idempotent(store):
    await store.put(operator_chat_id=111, operator_msg_id=5, user_id=999)
    await store.put(operator_chat_id=111, operator_msg_id=5, user_id=1000)
    assert await store.get_user(111, 5) == 1000


async def test_survives_new_connection(tmp_path):
    path = str(tmp_path / "persist.db")
    s1 = Storage(path)
    await s1.init()
    await s1.put(operator_chat_id=1, operator_msg_id=2, user_id=3)
    # A fresh Storage over the same file == a bot restart.
    s2 = Storage(path)
    assert await s2.get_user(1, 2) == 3
