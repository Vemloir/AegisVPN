"""Tariff plan management: list and price editing."""

from aiogram import F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import Plan

from .common import is_admin
from .keyboards import plan_list_keyboard
from .states import AdminStates

router = Router()


def _plans_text(plans: list[Plan]) -> str:
    lines = [f"{html.bold('Тарифы')}", ""]
    for plan in plans:
        status = "ON" if plan.is_active else "OFF"
        lines.append(f"ID: {plan.id} | {plan.days} дней | {plan.stars_price} Stars | {status}")
    return "\n".join(lines)


@router.callback_query(F.data == "admin_plans")
async def cq_admin_plans(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    async with async_session_maker() as session:
        plans = (await session.execute(select(Plan).order_by(Plan.id))).scalars().all()

    await call.message.edit_text(  # type: ignore
        _plans_text(plans),
        parse_mode="HTML",
        reply_markup=plan_list_keyboard(plans),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_plan_edit:"))
async def cq_admin_plan_edit(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    plan_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)

    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_plan_price)
    await state.update_data(plan_id=plan_id)
    await call.message.edit_text(  # type: ignore
        f"Тариф ID {plan.id}: {plan.days} дней.\nТекущая цена: {plan.stars_price} Stars.\n\n"
        "Отправьте новую цену числом.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Назад к тарифам", callback_data="admin_plans")]]
        ),
    )
    await call.answer()


@router.message(AdminStates.waiting_plan_price)
async def msg_admin_plan_price(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    plan_id = data.get("plan_id")
    if not plan_id:
        await state.clear()
        await message.answer("Тариф не выбран.")
        return

    try:
        new_price = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужна цена числом.")
        return

    if new_price <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return

    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            await state.clear()
            await message.answer("Тариф не найден.")
            return

        plan.stars_price = new_price
        await session.commit()
        plans = (await session.execute(select(Plan).order_by(Plan.id))).scalars().all()

    await state.clear()
    await message.answer("Цена обновлена.")
    await message.answer(
        _plans_text(plans),
        parse_mode="HTML",
        reply_markup=plan_list_keyboard(plans),
    )
