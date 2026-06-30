"""Tariff plan management: list, view, create, edit prices, delete."""

from aiogram import F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import Plan

from .common import is_admin
from .keyboards import confirmation_keyboard, plan_detail_keyboard, plan_list_keyboard
from .states import AdminStates

router = Router()


def _back_to_plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад к тарифам", callback_data="admin_plans")]]
    )


def _stars_str(plan: Plan) -> str:
    return f"{plan.stars_price} Stars" if plan.stars_price else "нет"


def _rub_str(plan: Plan) -> str:
    return f"{plan.rub_price}₽" if plan.rub_price else "нет"


def _plans_text(plans: list[Plan]) -> str:
    lines = [f"{html.bold('Тарифы')}", ""]
    for plan in plans:
        status = "ON" if plan.is_active else "OFF"
        lines.append(f"ID: {plan.id} | {plan.days} дней | {_stars_str(plan)} | {_rub_str(plan)} | {status}")
    return "\n".join(lines)


async def _show_plans(target: CallbackQuery | Message) -> None:
    async with async_session_maker() as session:
        plans = (await session.execute(select(Plan).order_by(Plan.id))).scalars().all()
    text = _plans_text(plans)
    keyboard = plan_list_keyboard(plans)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "admin_plans")
async def cq_admin_plans(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await _show_plans(call)


@router.callback_query(F.data.startswith("admin_plan_show:"))
async def cq_admin_plan_show(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    plan_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return

    status = "ON" if plan.is_active else "OFF"
    await call.message.edit_text(  # type: ignore
        f"{html.bold(f'Тариф ID {plan.id}')}\n\n"
        f"Срок: {plan.days} дней\n"
        f"Звёзды: {_stars_str(plan)}\n"
        f"Рубли (СБП): {_rub_str(plan)}\n"
        f"Статус: {status}",
        parse_mode="HTML",
        reply_markup=plan_detail_keyboard(plan.id),
    )
    await call.answer()


# --------------------------------------------------------------------------
# Create a new tariff: days -> stars price -> rub price
# --------------------------------------------------------------------------


@router.callback_query(F.data == "admin_plan_create")
async def cq_admin_plan_create(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_plan_new_days)
    await call.message.edit_text(  # type: ignore
        "Новый тариф.\nОтправьте срок в днях числом.",
        reply_markup=_back_to_plans_keyboard(),
    )
    await call.answer()


@router.message(AdminStates.waiting_plan_new_days)
async def msg_admin_plan_new_days(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        days = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число (дней).")
        return
    if days <= 0:
        await message.answer("Срок должен быть больше нуля.")
        return

    await state.update_data(new_days=days)
    await state.set_state(AdminStates.waiting_plan_new_stars)
    await message.answer("Цена в звёздах числом. 0 без оплаты звёздами.", reply_markup=_back_to_plans_keyboard())


@router.message(AdminStates.waiting_plan_new_stars)
async def msg_admin_plan_new_stars(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        stars = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужна цена в звёздах числом (0 без звёзд).")
        return
    if stars < 0:
        await message.answer("Цена не может быть отрицательной (0 без звёзд).")
        return

    await state.update_data(new_stars=stars)
    await state.set_state(AdminStates.waiting_plan_new_rub)
    await message.answer("Цена в рублях для СБП числом. 0 без СБП.", reply_markup=_back_to_plans_keyboard())


@router.message(AdminStates.waiting_plan_new_rub)
async def msg_admin_plan_new_rub(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        rub = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужна цена в рублях числом (0 без СБП).")
        return
    if rub < 0:
        await message.answer("Цена не может быть отрицательной (0 без СБП).")
        return

    data = await state.get_data()
    days = data.get("new_days")
    stars = data.get("new_stars")
    if not days or stars is None:
        await state.clear()
        await message.answer("Данные тарифа потеряны, начните заново.")
        await _show_plans(message)
        return
    if not stars and not rub:
        await message.answer("Нужна хотя бы одна цена: звёзды или рубли. Отправьте цену в рублях.")
        return

    async with async_session_maker() as session:
        session.add(Plan(days=days, stars_price=stars, rub_price=rub or None, is_active=True))
        await session.commit()

    await state.clear()
    await message.answer("Тариф создан.")
    await _show_plans(message)


# --------------------------------------------------------------------------
# Edit an existing tariff: stars price -> rub price
# --------------------------------------------------------------------------


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
        f"Тариф ID {plan.id}: {plan.days} дней.\n"
        f"Цена: {_stars_str(plan)} / {_rub_str(plan)}.\n\n"
        "Отправьте новую цену в звёздах числом. 0 без звёзд.",
        reply_markup=_back_to_plans_keyboard(),
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
        await message.answer("Нужна цена в звёздах числом (0 без звёзд).")
        return

    if new_price < 0:
        await message.answer("Цена не может быть отрицательной (0 без звёзд).")
        return

    await state.update_data(new_stars=new_price)
    await state.set_state(AdminStates.waiting_plan_rub)
    await message.answer("Цена в рублях для СБП числом. 0 без СБП.", reply_markup=_back_to_plans_keyboard())


@router.message(AdminStates.waiting_plan_rub)
async def msg_admin_plan_rub(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    plan_id = data.get("plan_id")
    new_stars = data.get("new_stars")
    if not plan_id or new_stars is None:
        await state.clear()
        await message.answer("Тариф не выбран.")
        return

    try:
        new_rub = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужна цена в рублях числом (0 без СБП).")
        return
    if new_rub < 0:
        await message.answer("Цена не может быть отрицательной (0 без СБП).")
        return
    if not new_stars and not new_rub:
        await message.answer("Нужна хотя бы одна цена: звёзды или рубли. Отправьте цену в рублях.")
        return

    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            await state.clear()
            await message.answer("Тариф не найден.")
            return

        plan.stars_price = new_stars
        plan.rub_price = new_rub or None
        await session.commit()

    await state.clear()
    await message.answer("Цена обновлена.")
    await _show_plans(message)


# --------------------------------------------------------------------------
# Delete a tariff
# --------------------------------------------------------------------------


@router.callback_query(F.data.startswith("admin_plan_delete:"))
async def cq_admin_plan_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    plan_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return

    await call.message.edit_text(  # type: ignore
        f"Удалить тариф ID {plan.id} ({plan.days} дней)?",
        reply_markup=confirmation_keyboard(
            confirm_data=f"admin_plan_delete_confirm:{plan.id}",
            cancel_data=f"admin_plan_show:{plan.id}",
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_plan_delete_confirm:"))
async def cq_admin_plan_delete_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    plan_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        if plan is not None:
            await session.delete(plan)
            await session.commit()

    await call.answer("Тариф удалён")
    await _show_plans(call)
