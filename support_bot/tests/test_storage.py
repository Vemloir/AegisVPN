import pytest

from src.storage import Storage


@pytest.fixture
async def store(tmp_path):
    s = Storage(str(tmp_path / "support.db"))
    await s.init()
    return s


async def test_create_and_fetch(store):
    tid = await store.create_ticket(42, "ivan", "Иван", "Тема", "Первое сообщение")
    t = await store.get_ticket(tid)
    assert t["status"] == "open" and t["title"] == "Тема" and t["user_id"] == 42
    msgs = await store.get_messages(tid)
    assert len(msgs) == 1 and msgs[0]["sender"] == "user" and msgs[0]["text"] == "Первое сообщение"


async def test_add_messages_ordered(store):
    tid = await store.create_ticket(1, None, "U", "T", "a")
    await store.add_message(tid, "operator", "b")
    await store.add_message(tid, "user", "c")
    msgs = await store.get_messages(tid)
    assert [m["text"] for m in msgs] == ["a", "b", "c"]
    assert [m["sender"] for m in msgs] == ["user", "operator", "user"]


async def test_close(store):
    tid = await store.create_ticket(1, None, "U", "T", "a")
    await store.close_ticket(tid)
    assert (await store.get_ticket(tid))["status"] == "closed"


async def test_count_and_pagination(store):
    for i in range(12):
        await store.create_ticket(1, None, "U", f"T{i}", "x")
    assert await store.count_tickets(1) == 12
    page0 = await store.list_tickets(1, offset=0, limit=5)
    page2 = await store.list_tickets(1, offset=10, limit=5)
    assert len(page0) == 5 and len(page2) == 2
    # no overlap between pages
    assert not ({t["id"] for t in page0} & {t["id"] for t in page2})


async def test_list_scoped_per_user(store):
    await store.create_ticket(1, None, "A", "T", "x")
    await store.create_ticket(2, None, "B", "T", "x")
    assert await store.count_tickets(1) == 1
    assert await store.count_tickets(2) == 1


async def test_open_tickets_sort_first(store):
    old = await store.create_ticket(1, None, "U", "old", "x")
    new = await store.create_ticket(1, None, "U", "new", "x")
    await store.close_ticket(new)  # newer but closed -> should rank below the open one
    ids = [t["id"] for t in await store.list_tickets(1, 0, 5)]
    assert ids[0] == old


async def test_lang_override(store):
    assert await store.get_lang(42) is None  # default -> follow main bot
    await store.set_lang(42, "en")
    assert await store.get_lang(42) == "en"
    await store.set_lang(42, "ru")  # overwrite
    assert await store.get_lang(42) == "ru"


async def test_admin_msg_map(store):
    tid = await store.create_ticket(1, None, "U", "T", "x")
    await store.set_admin_msg(operator_chat_id=555, operator_msg_id=9, ticket_id=tid)
    assert await store.ticket_by_admin_msg(555, 9) == tid
    assert await store.ticket_by_admin_msg(555, 404) is None
    assert await store.ticket_by_admin_msg(999, 9) is None
