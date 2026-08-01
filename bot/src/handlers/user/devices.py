"""Devices management screen: list, detail, suspend/resume, remove.

Shows the device name, OS (when the client reports it), the approximate location
it was added from, and the added time — plus suspend/resume/delete controls.
"""

import re
from datetime import UTC, datetime

from aiogram import F, Router, html
from aiogram.types import CallbackQuery
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import Device, Subscription, User
from src.services import get_user_language, t
from src.services.geoip import flag_emoji
from src.services.subscription_service import SubscriptionService

from .keyboards import (
    device_detail_keyboard,
    device_remove_confirm_keyboard,
    devices_list_keyboard,
)

# Strips a 3+ digit run (a client build number like Happ's) that an older parser
# may have stored as if it were an OS version — so stale records render cleanly
# without waiting for the self-heal on the next subscription fetch.
_BUILD_NUM_RE = re.compile(r"\s+\d{3,}")


def _clean_label(s: str | None) -> str | None:
    return _BUILD_NUM_RE.sub("", s) if s else s


router = Router()


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
            lines.append(f"{i}. {_clean_label(dev.display_name)}{status}")
    lines += ["", t(language, "devices_hint")]
    return "\n".join(lines), devices_list_keyboard(language, devices)


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

        name = _clean_label(device.display_name)
        os_label = _clean_label(device.os_label)
        build = device.build_number
        added = device.created_at.strftime("%d.%m.%Y, %H:%M")
        location = device.added_location
        country_code = device.added_country_code
        is_suspended = device.is_suspended
        dev_id = device.id

    lines = [html.bold(name), ""]
    # Always show the OS line. It carries a version when the client reports one
    # (e.g. "iOS 17"); for clients that don't (Happ sends just "Android"), it shows
    # the bare platform — the real OS version simply isn't in the User-Agent.
    if os_label:
        lines.append(t(language, "device_detail_os", os=os_label))
    if build:
        lines.append(t(language, "device_detail_build", build=build))
    lines.append(t(language, "device_detail_added", date=added))
    if location:
        flag = flag_emoji(country_code)
        loc = f"{flag} {location}".strip() if flag else location
        lines.append(t(language, "device_detail_location", location=loc))
    if is_suspended:
        lines.append(t(language, "device_detail_suspended"))

    return "\n".join(lines), device_detail_keyboard(language, dev_id, is_suspended)


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
