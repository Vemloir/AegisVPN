from src.render import render_admin_history, render_user_thread, ticket_button_label

TICKET = {"id": 7, "user_id": 42, "username": "ivan", "full_name": "Иван Петров", "title": "Не работает Германия", "status": "open"}
MSGS = [
    {"sender": "user", "text": "Германия не подключается"},
    {"sender": "operator", "text": "Проверьте обновление подписки"},
    {"sender": "user", "text": "Помогло, спасибо"},
]


def test_admin_history_has_contact_and_full_thread():
    out = render_admin_history(TICKET, MSGS)
    assert "Тикет #7 — Не работает Германия" in out
    assert "ID: 42, @ivan" in out
    assert "Статус: открыт" in out
    assert "Пользователь: Германия не подключается" in out
    assert "Поддержка: Проверьте обновление подписки" in out
    assert "Пользователь: Помогло, спасибо" in out


def test_admin_history_without_username():
    t = {**TICKET, "username": None}
    out = render_admin_history(t, MSGS)
    assert "ID: 42)" in out and "@" not in out.split("\n")[1]


def test_user_thread_hides_contact():
    out = render_user_thread(TICKET, MSGS)
    assert "Тикет #7" in out and "Статус: открыт" in out
    assert "ID: 42" not in out  # user view must not echo the contact line


def test_closed_status_label():
    out = render_user_thread({**TICKET, "status": "closed"}, MSGS)
    assert "Статус: закрыт" in out


def test_history_truncates_oldest_first():
    many = [{"sender": "user", "text": f"msg-{i}-" + "x" * 50} for i in range(200)]
    out = render_admin_history(TICKET, many, max_len=800)
    assert "ранние сообщения скрыты" in out
    assert "msg-199-" in out  # newest kept
    assert "msg-0-" not in out  # oldest dropped
    assert len(out) <= 1000


def test_button_label_truncates_long_title():
    label = ticket_button_label({"id": 3, "title": "x" * 80, "status": "open"})
    assert label.startswith("#3 ") and label.endswith("— открыт") and "…" in label
