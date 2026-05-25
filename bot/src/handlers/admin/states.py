from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    waiting_user_lookup = State()
    waiting_server_allow_user = State()
    waiting_server_revoke_user = State()
    waiting_plan_price = State()
    waiting_user_issue_days = State()
    waiting_bulk_extend_days = State()
