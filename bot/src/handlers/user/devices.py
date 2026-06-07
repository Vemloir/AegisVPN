"""Devices management screen: list, remove."""

from datetime import UTC, datetime

from aiogram import F, Router, html
from aiogram.types import CallbackQuery
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import Device, Subscription, User
from src.services import get_user_language, t
from src.services.subscription_service import SubscriptionService

from .keyboards import device_remove_confirm_keyboard, devices_list_keyboard

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


async def _get_sub_for_user(tg_id: int) -> tuple[User | None, Subscription | None]:
    now = datetime.now(UTC).replace(tzinfo=None)
    async with async_session_maker() as session:
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
    now = datetime.now(UTC).replace(tzinfo=None)
    async with async_session_maker() as session:
        user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if not user:
            return t(language, "access_unavailable"), devices_list_keyboard(language, [])

        sub = (
            await session.execute(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,  # noqa: E712
                    Subscription.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if not sub:
            return t(language, "no_active_subscription"), devices_list_keyboard(language, [])

        devices = await SubscriptionService.get_active_devices(session, sub)

    count = len(devices)
    lines = [html.bold(t(language, "devices_title", count=count)), ""]

    if not devices:
        lines.append(t(language, "devices_empty"))
    else:
        for i, dev in enumerate(devices, 1):
            lines.append(f"{i}. {dev.display_name} — {_fmt_last_active(language, dev.last_active_at)}")

    lines += ["", t(language, "devices_hint")]
    return "\n".join(lines), devices_list_keyboard(language, devices)


@router.callback_query(F.data == "devices_open")
async def cq_devices_open(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    text, markup = await _render_devices(call.from_user.id, language)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer()


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

    now = datetime.now(UTC).replace(tzinfo=None)
    async with async_session_maker() as session:
        user = (await session.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
        if not user:
            await call.answer(t(language, "access_unavailable"), show_alert=True)
            return

        sub = (
            await session.execute(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,  # noqa: E712
                    Subscription.expires_at > now,
                )
            )
        ).scalar_one_or_none()
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
