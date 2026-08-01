import asyncio
import contextlib

from aiogram import Bot, F, Router, html
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import select

from src.core.config import settings
from src.core.database import async_session_maker
from src.core.logger import logger
from src.models import Payment, Plan, User
from src.services import (
    SubscriptionService,
    UserService,
    apply_paid_subscription,
    get_user_language,
    t,
    user_grant_lock,
)
from src.services.platega_client import PlategaError, create_sbp_transaction

router = Router()


def plan_selection_keyboard(language: str, plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t(language, "plan_button_label", days=plan.days), callback_data=f"plan_{plan.id}")]
        for plan in plans
    ]
    rows.append([InlineKeyboardButton(text=t(language, "back"), callback_data="subscription_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_method_keyboard(language: str, plan: Plan) -> InlineKeyboardMarkup:
    rows = []
    if plan.stars_price:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(language, "pay_stars_button", stars=plan.stars_price),
                    callback_data=f"paystars_{plan.id}",
                )
            ]
        )
    if plan.rub_price:
        rub_key = "pay_sbp_button" if settings.platega_enabled else "pay_sbp_button_soon"
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(language, rub_key, rub=plan.rub_price),
                    callback_data=f"payrub_{plan.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=t(language, "back"), callback_data="buy_plan")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def no_plans_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(language, "back"), callback_data="subscription_open")]]
    )


@router.callback_query(F.data == "buy_plan")
async def show_plans(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    if not await UserService.is_privacy_accepted(call.from_user.id):
        await call.answer(t(language, "privacy_required"), show_alert=True)
        return
    has_active_subscription = await UserService.has_active_subscription(call.from_user.id)

    async with async_session_maker() as session:
        result = await session.execute(select(Plan).where(Plan.is_active == True).order_by(Plan.id))
        plans = result.scalars().all()

        if not plans:
            await call.message.edit_text(t(language, "no_plans_available"), reply_markup=no_plans_keyboard(language))  # type: ignore
            await call.answer()
            return

        key = "choose_plan_renew" if has_active_subscription else "choose_plan_buy"
        await call.message.edit_text(  # type: ignore
            t(language, key),
            reply_markup=plan_selection_keyboard(language, plans),
        )
        await call.answer()


@router.callback_query(F.data.startswith("plan_"))
async def choose_payment_method(call: CallbackQuery):
    plan_id = int(call.data.split("_")[1])  # type: ignore[index]
    language = await get_user_language(call.from_user.id)

    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        if not plan or not plan.is_active:
            await call.answer(t(language, "plan_unavailable"), show_alert=True)
            return

    await call.message.edit_text(  # type: ignore
        t(language, "choose_payment_method", days=plan.days),
        reply_markup=payment_method_keyboard(language, plan),
    )
    await call.answer()


_SBP_EXPIRY_TASKS: set[asyncio.Task] = set()


def _parse_expires_seconds(raw: str | None, default: int = 1800) -> int:
    """Parse Platega's ``expiresIn`` ("HH:MM:SS") to seconds; default 30 min."""
    try:
        h, m, s = (int(x) for x in (raw or "").split(":"))
        total = h * 3600 + m * 60 + s
        return total if total > 0 else default
    except (ValueError, TypeError):
        return default


async def _expire_sbp_invoice(bot: Bot, chat_id: int, message_id: int, payment_id: int, language: str, delay: int):
    """When the payment window closes, replace the "Оплатить" screen if the payment
    never confirmed (the Platega link is dead by then anyway)."""
    await asyncio.sleep(delay)
    async with async_session_maker() as session:
        payment = await session.get(Payment, payment_id)
    if payment is None or payment.status == "confirmed":
        return
    with contextlib.suppress(Exception):
        await bot.edit_message_text(
            t(language, "sbp_expired"),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=t(language, "back"), callback_data="buy_plan")]]
            ),
        )


@router.callback_query(F.data.startswith("payrub_"))
async def pay_rub_methods(call: CallbackQuery):
    """Ruble payment: let the user pick the method (СБП now, cards later)."""
    plan_id = int(call.data.split("_")[1])  # type: ignore[index]
    language = await get_user_language(call.from_user.id)
    if not settings.platega_enabled:
        await call.answer(t(language, "sbp_soon"), show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "pay_method_sbp"), callback_data=f"paysbp_{plan_id}")],
            [InlineKeyboardButton(text=t(language, "back"), callback_data=f"plan_{plan_id}")],
        ]
    )
    await call.message.edit_text(t(language, "choose_rub_method"), reply_markup=kb)  # type: ignore
    await call.answer()


@router.callback_query(F.data.startswith("paysbp_"))
async def pay_sbp(call: CallbackQuery):
    plan_id = int(call.data.split("_")[1])  # type: ignore[index]
    language = await get_user_language(call.from_user.id)

    if not settings.platega_enabled:
        await call.answer(t(language, "sbp_soon"), show_alert=True)
        return
    if not await UserService.is_privacy_accepted(call.from_user.id):
        await call.answer(t(language, "privacy_required"), show_alert=True)
        return

    has_active = await UserService.has_active_subscription(call.from_user.id)
    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        if not plan or not plan.is_active or not plan.rub_price:
            await call.answer(t(language, "plan_unavailable"), show_alert=True)
            return
        user = (await session.execute(select(User).where(User.tg_id == call.from_user.id))).scalar_one_or_none()
        if user is None:
            await call.answer(t(language, "plan_unavailable"), show_alert=True)
            return

        desc_key = "invoice_desc_renew" if has_active else "invoice_desc_buy"
        return_url = settings.bot_public_url or "https://t.me"
        try:
            resp = await create_sbp_transaction(
                amount_rub=plan.rub_price,
                description=t(language, desc_key, days=plan.days),
                payload=f"sbp_{plan.id}_{call.from_user.id}",
                user_id=call.from_user.id,
                username=call.from_user.username,
                return_url=return_url,
                failed_url=return_url,
            )
        except PlategaError as exc:
            logger.error(f"Platega create failed (user {call.from_user.id}, plan {plan_id}): {exc}")
            await call.answer(t(language, "sbp_error"), show_alert=True)
            return

        tx_id = resp.get("transactionId")
        redirect = resp.get("redirect")
        if not tx_id or not redirect:
            logger.error(f"Platega create returned no tx/redirect: {resp}")
            await call.answer(t(language, "sbp_error"), show_alert=True)
            return

        # Pending row: maps the transaction back to (user, plan_days) reliably and
        # is the idempotency key for the callback, independent of payload round-trip.
        payment = Payment(
            user_id=user.id,
            tg_payment_id=f"platega_{tx_id}",
            stars_amount=0,
            plan_days=plan.days,
            provider="platega",
            rub_amount=plan.rub_price,
            status="pending",
        )
        session.add(payment)
        await session.flush()
        payment_id = payment.id
        await session.commit()
        rub = plan.rub_price
        days = plan.days

    expire_seconds = _parse_expires_seconds(resp.get("expiresIn"))
    expire_minutes = max(1, round(expire_seconds / 60))

    # No "check payment" button: confirmation is fully automatic via the Platega
    # callback (POST /payment/platega/callback), with reconcile_pending_platega as
    # a background backstop for a missed callback.
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "pay_now"), url=redirect)],
            [InlineKeyboardButton(text=t(language, "back"), callback_data=f"plan_{plan_id}")],
        ]
    )
    await call.message.edit_text(  # type: ignore
        t(language, "sbp_invoice", rub=rub, days=days, mins=expire_minutes), reply_markup=kb
    )
    await call.answer()

    # Close the pay window when the Platega link expires. Kept referenced so the
    # task isn't garbage-collected mid-flight.
    task = asyncio.create_task(
        _expire_sbp_invoice(
            call.bot, call.message.chat.id, call.message.message_id, payment_id, language, expire_seconds
        )
    )
    _SBP_EXPIRY_TASKS.add(task)
    task.add_done_callback(_SBP_EXPIRY_TASKS.discard)


@router.callback_query(F.data.startswith("paystars_"))
async def create_invoice(call: CallbackQuery):
    plan_id = int(call.data.split("_")[1])  # type: ignore[index]
    language = await get_user_language(call.from_user.id)
    has_active_subscription = await UserService.has_active_subscription(call.from_user.id)

    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        if not plan or not plan.is_active or not plan.stars_price:
            await call.answer(t(language, "plan_unavailable"), show_alert=True)
            return

    payload_prefix = "renew_plan" if has_active_subscription else "buy_plan"
    payload = f"{payload_prefix}_{plan.id}_{call.from_user.id}"
    title_key = "invoice_title_renew" if has_active_subscription else "invoice_title_buy"
    desc_key = "invoice_desc_renew" if has_active_subscription else "invoice_desc_buy"
    label_key = "price_label_renew" if has_active_subscription else "price_label_buy"

    await call.message.answer_invoice(  # type: ignore
        title=t(language, title_key),
        description=t(language, desc_key, days=plan.days),
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=t(language, label_key, days=plan.days), amount=plan.stars_price)],
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    payload = pre_checkout_query.invoice_payload or ""
    if not (payload.startswith("buy_plan_") or payload.startswith("renew_plan_")):
        await pre_checkout_query.answer(ok=False, error_message="Invalid order")
        return

    parts = payload.split("_")
    try:
        plan_id = int(parts[2])
    except (IndexError, ValueError):
        await pre_checkout_query.answer(ok=False, error_message="Invalid order")
        return

    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None or not plan.is_active:
            await pre_checkout_query.answer(ok=False, error_message="Plan no longer available")
            return

        if plan.stars_price != pre_checkout_query.total_amount:
            await pre_checkout_query.answer(ok=False, error_message="Price mismatch, please retry")
            return

    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload  # type: ignore[assignment]

    if not (payload.startswith("buy_plan_") or payload.startswith("renew_plan_")):
        return

    parts = payload.split("_")
    plan_id = int(parts[2])
    tg_id = int(parts[3])

    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        plan = await session.get(Plan, plan_id)

        if not user or not plan:
            logger.error(f"Payment payload references missing data: user={tg_id}, plan={plan_id}")
            return

        session.add(
            Payment(
                user_id=user.id,
                tg_payment_id=payment_info.telegram_payment_charge_id,  # type: ignore[arg-type]
                stars_amount=payment_info.total_amount,  # type: ignore[arg-type]
                plan_days=plan.days,
                provider="stars",
            )
        )
        # Serialise the grant per-user so a concurrent Stars/Platega confirmation
        # can't lose one payment's extension (read-modify-write on expires_at).
        async with user_grant_lock(user.id):
            sub_token, is_renewal = await apply_paid_subscription(session, user, plan.days)
            await session.commit()

        language = user.language
        link = html.code(SubscriptionService.build_subscription_url(sub_token))
        action_key = "payment_action_renewed" if is_renewal else "payment_action_activated"
        text = t(language, "payment_success", days=plan.days, action_text=t(language, action_key), link=link)
        await message.answer(text, parse_mode="HTML")
