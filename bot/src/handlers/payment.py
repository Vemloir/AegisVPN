import uuid
from datetime import UTC, datetime, timedelta

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

from src.core.database import async_session_maker
from src.core.logger import logger
from src.models import Payment, Plan, Subscription, User
from src.services import (
    ServerAccessService,
    SubscriptionService,
    UserService,
    get_user_language,
    t,
)

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
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(language, "pay_sbp_button", rub=plan.rub_price),
                    callback_data=f"paysbp_{plan.id}",
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


@router.callback_query(F.data.startswith("paysbp_"))
async def pay_sbp_soon(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    await call.answer(t(language, "sbp_soon"), show_alert=True)


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
    is_renewal = payload.startswith("renew_plan_")

    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        plan = await session.get(Plan, plan_id)

        if not user or not plan:
            logger.error(f"Payment payload references missing data: user={tg_id}, plan={plan_id}")
            return

        payment = Payment(
            user_id=user.id,
            tg_payment_id=payment_info.telegram_payment_charge_id,  # type: ignore[arg-type]
            stars_amount=payment_info.total_amount,  # type: ignore[arg-type]
            plan_days=plan.days,
        )
        session.add(payment)

        now = datetime.now(UTC).replace(tzinfo=None)

        # Try to find an active subscription first
        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,
            )
        )
        active_sub = sub_result.scalar_one_or_none()

        if active_sub:
            # Renew existing active subscription (same URL)
            active_sub.expires_at = max(active_sub.expires_at, now) + timedelta(days=plan.days)
            sub_token = active_sub.sub_token
            is_renewal = True
        else:
            # No active subscription — try to restore the most recent expired one
            # (same client_uuid, same sub_token) so the URL stays the same
            expired_result = await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.expires_at.desc())
                .limit(1)
            )
            expired_sub = expired_result.scalar_one_or_none()

            if expired_sub and expired_sub.expires_at < now:
                # Restore the expired subscription: reactivate it with the same UUID/token
                expired_sub.is_active = True
                expired_sub.plan_days = plan.days
                expired_sub.expires_at = now + timedelta(days=plan.days)
                active_sub = expired_sub
                sub_token = expired_sub.sub_token
                is_renewal = True
            else:
                # No expired subscription to restore — create a brand new one
                sub_token = await SubscriptionService.generate_sub_token(session)
                client_uuid = str(uuid.uuid4())
                active_sub = Subscription(
                    user_id=user.id,
                    sub_token=sub_token,
                    client_uuid=client_uuid,
                    plan_days=plan.days,
                    started_at=now,
                    expires_at=now + timedelta(days=plan.days),
                    is_active=True,
                )
                session.add(active_sub)
                is_renewal = False

        await session.flush()
        active_servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
        await SubscriptionService.sync_subscription_to_servers(session, active_sub, active_servers)
        await session.commit()

        language = user.language
        link = html.code(SubscriptionService.build_subscription_url(sub_token))
        action_key = "payment_action_renewed" if is_renewal else "payment_action_activated"
        text = t(language, "payment_success", days=plan.days, action_text=t(language, action_key), link=link)
        await message.answer(text, parse_mode="HTML")
