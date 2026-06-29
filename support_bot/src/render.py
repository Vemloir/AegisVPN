"""Pure text renderers for tickets — no aiogram, fully testable.

User-facing views are localized (lang); the operator history stays Russian (the
operators are the RU team). Histories render newest-last and trim from the
OLDEST end so the most recent exchange always survives Telegram's 4096 cap.
"""

from __future__ import annotations

from .i18n import t

_MAX_LEN = 3500


def _render_thread(messages: list[dict], budget: int, who_user: str, who_support: str, truncated_label: str) -> str:
    lines = [f"{(who_user if m['sender'] == 'user' else who_support)}: {m['text']}" for m in messages]
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
        body = truncated_label + "\n\n" + body
    return body


def _status_word(ticket: dict, lang: str) -> str:
    return t(lang, "st_open") if ticket.get("status") == "open" else t(lang, "st_closed")


def render_admin_history(ticket: dict, messages: list[dict], max_len: int = _MAX_LEN) -> str:
    """Operator-facing (RU): who wrote + status + full thread, one message."""
    handle = f", @{ticket['username']}" if ticket.get("username") else ""
    status = "открыт" if ticket.get("status") == "open" else "закрыт"
    header = (
        f"Тикет #{ticket['id']} — {ticket['title']}\n"
        f"От: {ticket['full_name']} (ID: {ticket['user_id']}{handle})\n"
        f"Статус: {status}"
    )
    thread = _render_thread(messages, max_len - len(header) - 2, "Пользователь", "Поддержка", "… (ранние сообщения скрыты)")
    return header + "\n\n" + thread


def render_user_thread(ticket: dict, messages: list[dict], lang: str, max_len: int = _MAX_LEN) -> str:
    """User-facing localized thread (no operator contact details)."""
    header = f"{t(lang, 'thread_ticket')} #{ticket['id']} — {ticket['title']}\n{t(lang, 'status_label')}: {_status_word(ticket, lang)}"
    thread = _render_thread(
        messages, max_len - len(header) - 2, t(lang, "who_you"), t(lang, "who_support"), t(lang, "truncated")
    )
    return header + "\n\n" + thread


def ticket_button_label(ticket: dict, lang: str, max_title: int = 30) -> str:
    title = ticket["title"] if len(ticket["title"]) <= max_title else ticket["title"][: max_title - 1] + "…"
    return f"#{ticket['id']} {title} — {_status_word(ticket, lang)}"
