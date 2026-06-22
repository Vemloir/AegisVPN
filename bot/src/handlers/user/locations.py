"""Per-location settings screen ("Локации").

A drill-down: the per-location screen shows the current protocol (and, on VLESS,
the current transport) as buttons that open dedicated choosers. The choice is
persisted as a ``ServerTransportPref`` row (keyed by user+server) and re-applied
on every subscription fetch — the subscription URL/token never changes. A
location with no alternative transports (every node except Greece today) shows a
short "standard only" notice instead of the selectors.

Hysteria2 is shown but disabled: there is no Hy2 backend yet, so tapping it only
flashes a "coming soon" answer and never persists. The bot therefore can never
be coerced into emitting a Hy2 config.

Callbacks:
  loc:<sid>                       per-location screen
  loc_proto:<sid>                 protocol chooser
  loc_proto_set:<sid>:<proto>     persist protocol, re-render the location
  loc_transport:<sid>             transport chooser (vless only)
  loc_transport_set:<sid>:<tr>    persist transport, re-render the location
  loc_hy2:<sid>                   disabled-Hy2 alert (never persists)
"""

from aiogram import F, Router, html
from aiogram.types import CallbackQuery
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import Server, User
from src.services import UserService, get_user_language, t
from src.services.server_access_service import ServerAccessService
from src.services.subscription_service import SubscriptionService

from .keyboards import (
    location_no_alt_keyboard,
    location_protocol_keyboard,
    location_settings_keyboard,
    location_transport_keyboard,
    locations_list_keyboard,
)

router = Router()


async def _active_servers_for(session, tg_id: int) -> tuple["User | None", list[Server]]:
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if not user:
        return None, []
    servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
    servers = sorted(
        [s for s in servers if s.is_active], key=SubscriptionService.server_sort_key
    )
    return user, servers


@router.callback_query(F.data == "locations_open")
async def cq_locations_open(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    if not await UserService.has_active_subscription(call.from_user.id):
        await call.answer(t(language, "no_active_subscription"), show_alert=True)
        return
    async with async_session_maker() as session:
        _, servers = await _active_servers_for(session, call.from_user.id)

    if not servers:
        text = f"{html.bold(t(language, 'locations_title'))}\n\n{t(language, 'locations_none')}"
        await call.message.edit_text(  # type: ignore[union-attr]
            text, parse_mode="HTML", reply_markup=locations_list_keyboard(language, [])
        )
        await call.answer()
        return

    text = f"{html.bold(t(language, 'locations_title'))}\n\n{t(language, 'locations_intro')}"
    await call.message.edit_text(  # type: ignore[union-attr]
        text, parse_mode="HTML", reply_markup=locations_list_keyboard(language, servers)
    )
    await call.answer()


async def _load_location(call: CallbackQuery, server_id: int, language: str):
    """Re-run every access guard and load the current pref for a location.

    Returns ``(server, protocol, transport, available)`` on success, or ``None``
    after answering the callback with the "no such location" alert — so a user
    can never poke at a location they lost, an inactive node, or one without alt
    transports. Every entry point (per-location screen + both choosers + both
    setters) goes through this so the guards stay identical.
    """
    async with async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.tg_id == call.from_user.id))
        ).scalar_one_or_none()
        server = await session.get(Server, server_id)
        if user is None or server is None or not server.is_active:
            await call.answer(t(language, "locations_none"), show_alert=True)
            return None
        # Re-check access so a user can't poke at a location they lost.
        accessible = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
        if server.id not in {s.id for s in accessible}:
            await call.answer(t(language, "locations_none"), show_alert=True)
            return None
        if not server.has_alt_transports:
            label = SubscriptionService.format_server_label(server)
            text = (
                f"{html.bold(t(language, 'location_settings_title', name=label))}\n\n"
                f"{t(language, 'location_no_alt')}"
            )
            await call.message.edit_text(  # type: ignore[union-attr]
                text, parse_mode="HTML", reply_markup=location_no_alt_keyboard(language)
            )
            await call.answer()
            return None
        protocol, transport = await SubscriptionService.get_transport_pref(session, user.id, server.id)
        available = SubscriptionService.available_transports(server)
        return server, protocol, transport, available


async def _render_location(call: CallbackQuery, server_id: int, toast: str | None = None) -> None:
    language = await get_user_language(call.from_user.id)
    loaded = await _load_location(call, server_id, language)
    if loaded is None:
        return
    server, protocol, transport, available = loaded
    label = SubscriptionService.format_server_label(server)
    text = f"{html.bold(t(language, 'location_settings_title', name=label))}"
    await call.message.edit_text(  # type: ignore[union-attr]
        text,
        parse_mode="HTML",
        reply_markup=location_settings_keyboard(language, server.id, protocol, transport, available),
    )
    await call.answer(toast or "")


@router.callback_query(F.data.startswith("loc:"))
async def cq_location_open(call: CallbackQuery):
    server_id = int(call.data.split(":", 1)[1])  # type: ignore[union-attr]
    await _render_location(call, server_id)


@router.callback_query(F.data.startswith("loc_proto:"))
async def cq_location_protocol(call: CallbackQuery):
    """Protocol chooser for one location."""
    server_id = int(call.data.split(":", 1)[1])  # type: ignore[union-attr]
    language = await get_user_language(call.from_user.id)
    loaded = await _load_location(call, server_id, language)
    if loaded is None:
        return
    server, protocol, _transport, _available = loaded
    label = SubscriptionService.format_server_label(server)
    text = f"{html.bold(t(language, 'location_settings_title', name=label))}"
    await call.message.edit_text(  # type: ignore[union-attr]
        text,
        parse_mode="HTML",
        reply_markup=location_protocol_keyboard(language, server.id, protocol),
    )
    await call.answer()


@router.callback_query(F.data.startswith("loc_transport:"))
async def cq_location_transport(call: CallbackQuery):
    """Transport chooser — only reachable while the protocol is vless."""
    server_id = int(call.data.split(":", 1)[1])  # type: ignore[union-attr]
    language = await get_user_language(call.from_user.id)
    loaded = await _load_location(call, server_id, language)
    if loaded is None:
        return
    server, protocol, transport, available = loaded
    # The transport screen has no meaning off VLESS; bounce back to the location.
    if protocol != SubscriptionService.PROTOCOL_VLESS:
        await _render_location(call, server_id)
        return
    label = SubscriptionService.format_server_label(server)
    text = f"{html.bold(t(language, 'location_settings_title', name=label))}"
    await call.message.edit_text(  # type: ignore[union-attr]
        text,
        parse_mode="HTML",
        reply_markup=location_transport_keyboard(language, server.id, transport, available),
    )
    await call.answer()


@router.callback_query(F.data.startswith("loc_proto_set:"))
async def cq_location_protocol_set(call: CallbackQuery):
    # loc_proto_set:<server_id>:<protocol>
    _, raw_id, protocol = call.data.split(":", 2)  # type: ignore[union-attr]
    server_id = int(raw_id)
    language = await get_user_language(call.from_user.id)

    # Defense-in-depth: never persist a non-vless protocol (no Hy2 backend).
    if protocol != SubscriptionService.PROTOCOL_VLESS:
        await call.answer(t(language, "location_hy2_unavailable"), show_alert=True)
        return

    loaded = await _load_location(call, server_id, language)
    if loaded is None:
        return
    server, _protocol, transport, _available = loaded
    async with async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.tg_id == call.from_user.id))
        ).scalar_one_or_none()
        if user is None:
            await call.answer(t(language, "locations_none"), show_alert=True)
            return
        # Selecting vless keeps the current transport (which is always vless's own
        # since hy2 never persists); set_transport_pref collapses vless/xhttp to
        # "no row" automatically.
        await SubscriptionService.set_transport_pref(
            session, user.id, server.id, SubscriptionService.PROTOCOL_VLESS, transport
        )

    await _render_location(call, server_id, toast=t(language, "location_saved"))


@router.callback_query(F.data.startswith("loc_transport_set:"))
async def cq_location_transport_set(call: CallbackQuery):
    # loc_transport_set:<server_id>:<transport>
    _, raw_id, transport = call.data.split(":", 2)  # type: ignore[union-attr]
    server_id = int(raw_id)
    language = await get_user_language(call.from_user.id)

    loaded = await _load_location(call, server_id, language)
    if loaded is None:
        return
    server, _protocol, _transport, available = loaded
    # Only accept a transport the server actually serves; anything else is
    # ignored (the resolver would fall back to xhttp anyway). Selecting xhttp is
    # the reset — set_transport_pref deletes the row for the plain default.
    if transport not in available:
        transport = SubscriptionService.DEFAULT_TRANSPORT
    async with async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.tg_id == call.from_user.id))
        ).scalar_one_or_none()
        if user is None:
            await call.answer(t(language, "locations_none"), show_alert=True)
            return
        await SubscriptionService.set_transport_pref(
            session, user.id, server.id, SubscriptionService.PROTOCOL_VLESS, transport
        )

    await _render_location(call, server_id, toast=t(language, "location_saved"))


@router.callback_query(F.data.startswith("loc_hy2:"))
async def cq_location_hy2_disabled(call: CallbackQuery):
    """Hysteria2 is visible but not yet deployed — never persists a Hy2 pref."""
    language = await get_user_language(call.from_user.id)
    await call.answer(t(language, "location_hy2_unavailable"), show_alert=True)
