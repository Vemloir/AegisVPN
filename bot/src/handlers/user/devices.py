"""Devices management screen: list, detail, suspend/resume, remove."""

from datetime import UTC, datetime

from aiogram import F, Router, html
from aiogram.types import CallbackQuery
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import Device, Subscription, User
from src.services import get_user_language, t
from src.services.subscription_service import SubscriptionService

from .keyboards import device_detail_keyboard, device_remove_confirm_keyboard, devices_list_keyboard

router = Router()


def _fmt_last_active(language: str, dt: datetime | None) -> str:
    if dt is None:
        return t(language, "devices_active_never")
    diff = (datetime.now(UTC).replace(tzinfo=None) - dt).total_seconds()
    if diff < 90:
        return t(language, "devices_active_now")
    if diff < 3600:
        return t(language, "devices_active_min", n=int(diff // 60))
    if diff < 86400:
        return t(language, "devices_active_hours", n=int(diff // 3600))
    return t(language, "devices_active_days", n=int(diff // 86400))


async def _get_sub_for_user(session, tg_id: int) -> tuple["User | None", "Subscription | None"]:
    now = datetime.now(UTC).replace(tzinfo=None)
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if not user:
        return None, None
    sub = (
        await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,  # noqa: E712
                Subscription.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    return user, sub


async def _render_devices(tg_id: int, language: str) -> tuple[str, object]:
    async with async_session_maker() as session:
        _, sub = await _get_sub_for_user(session, tg_id)
        if not sub:
            return t(language, "no_active_subscription"), devices_list_keyboard(language, [])
        devices = await SubscriptionService.get_active_devices(session, sub)

    count = len(devices)
    lines = [html.bold(t(language, "devices_title", count=count)), ""]
    if not devices:
        lines.append(t(language, "devices_empty"))
    else:
        for i, dev in enumerate(devices, 1):
            status = " [II]" if dev.is_suspended else ""
            lines.append(f"{i}. {dev.display_name}{status} — {_fmt_last_active(language, dev.last_active_at)}")
    lines += ["", t(language, "devices_hint")]
    return "\n".join(lines), devices_list_keyboard(language, devices)


_CONNECTED_THRESHOLD_SEC = 5 * 60  # 5 minutes without traffic = not connected


async def _render_device_detail(tg_id: int, language: str, device_id: int) -> tuple[str, object] | None:
    async with async_session_maker() as session:
        _, sub = await _get_sub_for_user(session, tg_id)
        if not sub:
            return None

        device = (
            await session.execute(
                select(Device).where(Device.id == device_id, Device.subscription_id == sub.id, Device.is_active == True)  # noqa: E712
            )
        ).scalar_one_or_none()
        if not device:
            return None

        last_server = device.last_server if device.last_server_id else None

    lines = [html.bold(device.display_name), ""]
    lines.append(t(language, "device_detail_added", date=device.created_at.strftime("%d.%m.%Y, %H:%M")))

    if device.is_suspended:
        lines.append(t(language, "device_detail_suspended"))
    else:
        now = datetime.now(UTC).replace(tzinfo=None)
        is_online = (
            device.last_active_at is not None
            and (now - device.last_active_at).total_seconds() < _CONNECTED_THRESHOLD_SEC
            and last_server is not None
        )
        if is_online:
            loc = f"{last_server.flag} {last_server.name}".strip() if last_server.flag else last_server.name
            lines.append(t(language, "device_detail_connected", location=loc))
        else:
            lines.append(t(language, "device_detail_not_connected"))

    return "\n".join(lines), device_detail_keyboard(language, device.id, device.is_suspended)


@router.callback_query(F.data == "devices_open")
async def cq_devices_open(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    text, markup = await _render_devices(call.from_user.id, language)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer()


@router.callback_query(F.data.startswith("devices_detail:"))
async def cq_device_detail(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    try:
        device_id = int(call.data.split(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await call.answer()
        return
    rendered = await _render_device_detail(call.from_user.id, language, device_id)
    if rendered is None:
        await call.answer(t(language, "devices_not_found"), show_alert=True)
        return
    text, markup = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer()


@router.callback_query(F.data.startswith("devices_suspend:"))
async def cq_device_suspend(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    try:
        device_id = int(call.data.split(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await call.answer()
        return

    async with async_session_maker() as session:
        _, sub = await _get_sub_for_user(session, call.from_user.id)
        if not sub:
            await call.answer(t(language, "no_active_subscription"), show_alert=True)
            return
        device = (
            await session.execute(
                select(Device).where(Device.id == device_id, Device.subscription_id == sub.id, Device.is_active == True)  # noqa: E712
            )
        ).scalar_one_or_none()
        if device is None:
            await call.answer(t(language, "devices_not_found"), show_alert=True)
            return
        await SubscriptionService.suspend_device(session, sub, device)
        await session.commit()

    rendered = await _render_device_detail(call.from_user.id, language, device_id)
    if rendered:
        text, markup = rendered
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer(t(language, "device_suspended"), show_alert=True)


@router.callback_query(F.data.startswith("devices_resume:"))
async def cq_device_resume(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    try:
        device_id = int(call.data.split(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await call.answer()
        return

    async with async_session_maker() as session:
        _, sub = await _get_sub_for_user(session, call.from_user.id)
        if not sub:
            await call.answer(t(language, "no_active_subscription"), show_alert=True)
            return
        device = (
            await session.execute(
                select(Device).where(Device.id == device_id, Device.subscription_id == sub.id, Device.is_active == True)  # noqa: E712
            )
        ).scalar_one_or_none()
        if device is None:
            await call.answer(t(language, "devices_not_found"), show_alert=True)
            return
        await SubscriptionService.resume_device(session, sub, device)
        await session.commit()

    rendered = await _render_device_detail(call.from_user.id, language, device_id)
    if rendered:
        text, markup = rendered
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer(t(language, "device_resumed"), show_alert=True)


@router.callback_query(F.data.startswith("devices_remove:"))
async def cq_devices_remove(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    try:
        device_id = int(call.data.split(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await call.answer()
        return

    async with async_session_maker() as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id, Device.is_active == True))  # noqa: E712
        ).scalar_one_or_none()

    if device is None:
        await call.answer(t(language, "devices_not_found"), show_alert=True)
        return

    await call.message.edit_text(  # type: ignore
        t(language, "devices_remove_confirm", name=device.display_name),
        parse_mode="HTML",
        reply_markup=device_remove_confirm_keyboard(language, device_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("devices_remove_confirm:"))
async def cq_devices_remove_confirm(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    try:
        device_id = int(call.data.split(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await call.answer()
        return

    async with async_session_maker() as session:
        _, sub = await _get_sub_for_user(session, call.from_user.id)
        if not sub:
            await call.answer(t(language, "no_active_subscription"), show_alert=True)
            return
        device = (
            await session.execute(
                select(Device).where(Device.id == device_id, Device.subscription_id == sub.id, Device.is_active == True)  # noqa: E712
            )
        ).scalar_one_or_none()
        if device is None:
            await call.answer(t(language, "devices_not_found"), show_alert=True)
            return
        await SubscriptionService.remove_device(session, sub, device)
        await session.commit()

    text, markup = await _render_devices(call.from_user.id, language)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer(t(language, "devices_removed"), show_alert=True)
