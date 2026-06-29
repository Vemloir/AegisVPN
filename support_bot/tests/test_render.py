from src.render import render_admin_history, render_user_thread, ticket_button_label

TICKET = {"id": 7, "user_id": 42, "username": "ivan", "full_name": "Иван Петров", "title": "Не работает Германия", "status": "open"}
MSGS = [
    {"sender": "user", "text": "Германия не подключается"},
    {"sender": "operator", "text": "Проверьте обновление подписки"},
    {"sender": "user", "text": "Помогло, спасибо"},
]


def test_admin_history_is_russian_with_contact():
    out = render_admin_history(TICKET, MSGS)
    assert "Тикет #7 — Не работает Германия" in out
    assert "ID: 42, @ivan" in out
    assert "Статус: открыт" in out
    assert "Пользователь: Германия не подключается" in out
    assert "Поддержка: Проверьте обновление подписки" in out


def test_admin_history_without_username():
    out = render_admin_history({**TICKET, "username": None}, MSGS)
    assert "ID: 42)" in out


def test_user_thread_ru():
    out = render_user_thread(TICKET, MSGS, "ru")
    assert "Тикет #7" in out and "Статус: открыт" in out
    assert "Вы: Германия не подключается" in out and "Поддержка:" in out
    assert "ID: 42" not in out  # user view hides the contact line


def test_user_thread_en():
    out = render_user_thread(TICKET, MSGS, "en")
    assert "Ticket #7" in out and "Status: open" in out
    assert "You: Германия не подключается" in out and "Support:" in out


def test_closed_status_localized():
    assert "Статус: закрыт" in render_user_thread({**TICKET, "status": "closed"}, MSGS, "ru")
    assert "Status: closed" in render_user_thread({**TICKET, "status": "closed"}, MSGS, "en")


def test_history_truncates_oldest_first():
    many = [{"sender": "user", "text": f"msg-{i}-" + "x" * 50} for i in range(200)]
    out = render_admin_history(TICKET, many, max_len=800)
    assert "ранние сообщения скрыты" in out
    assert "msg-199-" in out and "msg-0-" not in out
    assert len(out) <= 1000


def test_button_label_localized_and_truncated():
    ru = ticket_button_label({"id": 3, "title": "x" * 80, "status": "open"}, "ru")
    en = ticket_button_label({"id": 3, "title": "y" * 80, "status": "closed"}, "en")
    assert ru.startswith("#3 ") and ru.endswith("— открыт") and "…" in ru
    assert en.endswith("— closed")
