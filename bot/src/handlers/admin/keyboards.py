"""Inline keyboard builders and small display helpers for the admin panel."""

from collections import Counter

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.models import Plan, Server, User
from src.services import SubscriptionService


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="Серверы", callback_data="admin_servers")],
            [InlineKeyboardButton(text="Тарифы", callback_data="admin_plans")],
            [InlineKeyboardButton(text="Пользователи", callback_data="admin_users")],
            [InlineKeyboardButton(text="Продлить всем активным", callback_data="admin_bulk_extend_active_start")],
            [InlineKeyboardButton(text="Скачать бекапп", callback_data="admin_download_db")],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")]]
    )


def admin_stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="admin_stats")],
            [InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")],
        ]
    )


def build_duplicate_name_keys(servers: list[Server]) -> set[str]:
    counts = Counter(server.name.strip().casefold() for server in servers if server.name.strip())
    return {name for name, count in counts.items() if count > 1}


def server_access_text(server: Server) -> str:
    if server.access_mode == "public":
        return "Доступен всем"
    return f"Ограниченный доступ ({len(server.access_grants)} пользователей)"


def server_list_keyboard(servers: list[Server]) -> InlineKeyboardMarkup:
    duplicate_name_keys = build_duplicate_name_keys(servers)
    rows = [
        [
            InlineKeyboardButton(
                text=SubscriptionService.format_server_label(server, duplicate_name_keys),
                callback_data=f"admin_server_manage:{server.id}",
            )
        ]
        for server in servers
    ]
    rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def server_manage_keyboard(server: Server) -> InlineKeyboardMarkup:
    toggle_text = "Сделать доступным всем" if server.access_mode == "restricted" else "Ограничить доступ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=f"admin_server_toggle:{server.id}")],
            [
                InlineKeyboardButton(
                    text="Разрешить пользователю", callback_data=f"admin_server_allow_start:{server.id}"
                )
            ],
            [InlineKeyboardButton(text="Забрать доступ", callback_data=f"admin_server_revoke_start:{server.id}")],
            [InlineKeyboardButton(text="К списку серверов", callback_data="admin_servers")],
        ]
    )


def users_lookup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")]]
    )


def plan_list_keyboard(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"ID {plan.id}: {plan.days} days - {plan.stars_price} Stars",
                callback_data=f"admin_plan_edit:{plan.id}",
            )
        ]
        for plan in plans
    ]
    rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_manage_keyboard(user: User, has_active_subscription: bool) -> InlineKeyboardMarkup:
    issue_text = "Продлить подписку" if has_active_subscription else "Выдать подписку"
    ban_text = "Разбанить" if user.is_banned else "Забанить"
    rows = [
        [InlineKeyboardButton(text=issue_text, callback_data=f"admin_user_issue_start:{user.tg_id}")],
        [InlineKeyboardButton(text="Выдать навсегда", callback_data=f"admin_user_issue_lifetime_start:{user.tg_id}")],
        [InlineKeyboardButton(text="Удалить подписку", callback_data=f"admin_user_revoke_prompt:{user.tg_id}")],
        [InlineKeyboardButton(text="Лимит подключений", callback_data=f"admin_user_connlimit:{user.tg_id}")],
        [InlineKeyboardButton(text=ban_text, callback_data=f"admin_user_toggle_ban:{user.tg_id}")],
        [InlineKeyboardButton(text="Другой пользователь", callback_data="admin_users")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_conn_limit_keyboard(tg_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Без лимита (∞)", callback_data=f"admin_user_connlimit_set:{tg_id}:0")],
        [InlineKeyboardButton(text="Задать число", callback_data=f"admin_user_connlimit_custom:{tg_id}")],
        [InlineKeyboardButton(text="Назад", callback_data=f"admin_user_show:{tg_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirmation_keyboard(confirm_data: str, cancel_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data=confirm_data),
                InlineKeyboardButton(text="Отмена", callback_data=cancel_data),
            ]
        ]
    )


def cancel_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=callback_data)]])
