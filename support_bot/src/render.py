"""Pure text renderers for tickets — no aiogram, fully testable.

Telegram caps a message at 4096 chars; histories are rendered newest-last and
trimmed from the OLDEST end so the most recent exchange always survives.
"""

from __future__ import annotations

_MAX_LEN = 3500


def _who(sender: str) -> str:
    return "Пользователь" if sender == "user" else "Поддержка"


def _status(ticket: dict) -> str:
    return "открыт" if ticket.get("status") == "open" else "закрыт"


def _render_thread(messages: list[dict], budget: int) -> str:
    lines = [f"{_who(m['sender'])}: {m['text']}" for m in messages]
    kept: list[str] = []
    total = 0
    truncated = False
    for line in reversed(lines):  # keep newest, drop oldest first
        if kept and total + len(line) + 2 > budget:
            truncated = True
            break
        kept.append(line)
        total += len(line) + 2
    kept.reverse()
    body = "\n\n".join(kept)
    if truncated:
        body = "… (ранние сообщения скрыты)\n\n" + body
    return body


def render_admin_history(ticket: dict, messages: list[dict], max_len: int = _MAX_LEN) -> str:
    """Operator-facing: who wrote + status + full thread (one consolidated message)."""
    handle = f", @{ticket['username']}" if ticket.get("username") else ""
    header = (
        f"Тикет #{ticket['id']} — {ticket['title']}\n"
        f"От: {ticket['full_name']} (ID: {ticket['user_id']}{handle})\n"
        f"Статус: {_status(ticket)}"
    )
    return header + "\n\n" + _render_thread(messages, max_len - len(header) - 2)


def render_user_thread(ticket: dict, messages: list[dict], max_len: int = _MAX_LEN) -> str:
    """User-facing thread view (no operator's contact details)."""
    header = f"Тикет #{ticket['id']} — {ticket['title']}\nСтатус: {_status(ticket)}"
    return header + "\n\n" + _render_thread(messages, max_len - len(header) - 2)


def ticket_button_label(ticket: dict, max_title: int = 30) -> str:
    title = ticket["title"] if len(ticket["title"]) <= max_title else ticket["title"][: max_title - 1] + "…"
    return f"#{ticket['id']} {title} — {_status(ticket)}"
