from aiogram.fsm.state import State, StatesGroup


class CreateTicket(StatesGroup):
    title = State()  # waiting for the ticket subject
    body = State()   # waiting for the first message


class ReplyTicket(StatesGroup):
    body = State()   # waiting for a follow-up message (ticket_id in state data)
