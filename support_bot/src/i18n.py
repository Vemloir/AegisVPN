"""Minimal i18n for the support bot (ru/en). User-facing strings only — operator
notifications stay Russian (the operators are the RU-speaking team)."""

from __future__ import annotations

SUPPORTED = ("ru", "en")


def normalize_lang(lang: str | None) -> str:
    return lang if lang in SUPPORTED else "ru"


TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": {
        "welcome": (
            "Это поддержка AegisVPN. Здесь можно создать тикет и переписываться с поддержкой.\n\n"
            "«Создать тикет» — новое обращение.\n"
            "«Мои тикеты» — список обращений и переписка."
        ),
        "btn_create": "Создать тикет",
        "btn_my": "Мои тикеты",
        "btn_back_menu": "Назад в меню",
        "btn_back_list": "Назад к списку",
        "btn_write": "Написать сообщение",
        "btn_close": "Закрыть тикет",
        "nav_prev": "« Пред.",
        "nav_next": "След. »",
        "page": "стр. {cur}/{total}",
        "prompt_title": "Введите короткое название тикета (тему):",
        "prompt_body": "Теперь опишите ваше обращение одним сообщением:",
        "title_empty": "Название не может быть пустым. Введите тему тикета:",
        "body_empty": "Сообщение не может быть пустым. Опишите обращение:",
        "created": "Тикет #{id} создан. Поддержка ответит здесь.",
        "list_title": "Ваши тикеты:",
        "list_empty": "У вас пока нет тикетов. Нажмите «Создать тикет».",
        "not_found": "Тикет не найден",
        "closed_alert": "Тикет закрыт",
        "reply_prompt": "Тикет #{id}: введите сообщение для поддержки:",
        "reply_empty": "Сообщение не может быть пустым:",
        "reply_added": "Сообщение добавлено в тикет #{id}.",
        "closed_toast": "Тикет закрыт",
        "closed_by_support": "Ваш тикет #{id} закрыт поддержкой.",
        "support_reply": "Ответ поддержки по тикету #{id}:",
        "use_menu": "Используйте меню ниже.",
        "thread_ticket": "Тикет",
        "status_label": "Статус",
        "st_open": "открыт",
        "st_closed": "закрыт",
        "who_you": "Вы",
        "who_support": "Поддержка",
        "truncated": "… (ранние сообщения скрыты)",
        "settings_title": "Язык интерфейса: {lang}",
        "lang_name": "Русский",
        "btn_lang_ru": "Русский",
        "btn_lang_en": "English",
        "settings_saved": "Язык сохранён.",
        "cmd_start": "Меню",
        "cmd_new": "Создать тикет",
        "cmd_tickets": "Мои тикеты",
        "cmd_settings": "Язык",
    },
    "en": {
        "welcome": (
            "This is AegisVPN support. You can open a ticket and chat with support here.\n\n"
            "“Create ticket” — a new request.\n"
            "“My tickets” — your tickets and the conversation."
        ),
        "btn_create": "Create ticket",
        "btn_my": "My tickets",
        "btn_back_menu": "Back to menu",
        "btn_back_list": "Back to list",
        "btn_write": "Write a message",
        "btn_close": "Close ticket",
        "nav_prev": "« Prev",
        "nav_next": "Next »",
        "page": "page {cur}/{total}",
        "prompt_title": "Enter a short ticket title (subject):",
        "prompt_body": "Now describe your request in a single message:",
        "title_empty": "The title cannot be empty. Enter the ticket subject:",
        "body_empty": "The message cannot be empty. Describe your request:",
        "created": "Ticket #{id} created. Support will reply here.",
        "list_title": "Your tickets:",
        "list_empty": "You have no tickets yet. Tap “Create ticket”.",
        "not_found": "Ticket not found",
        "closed_alert": "Ticket is closed",
        "reply_prompt": "Ticket #{id}: enter your message for support:",
        "reply_empty": "The message cannot be empty:",
        "reply_added": "Message added to ticket #{id}.",
        "closed_toast": "Ticket closed",
        "closed_by_support": "Your ticket #{id} has been closed by support.",
        "support_reply": "Support reply on ticket #{id}:",
        "use_menu": "Use the menu below.",
        "thread_ticket": "Ticket",
        "status_label": "Status",
        "st_open": "open",
        "st_closed": "closed",
        "who_you": "You",
        "who_support": "Support",
        "truncated": "… (earlier messages hidden)",
        "settings_title": "Interface language: {lang}",
        "lang_name": "English",
        "btn_lang_ru": "Русский",
        "btn_lang_en": "English",
        "settings_saved": "Language updated.",
        "cmd_start": "Menu",
        "cmd_new": "Create ticket",
        "cmd_tickets": "My tickets",
        "cmd_settings": "Language",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = normalize_lang(lang)
    template = TRANSLATIONS[lang].get(key) or TRANSLATIONS["ru"].get(key) or key
    return template.format(**kwargs) if kwargs else template
