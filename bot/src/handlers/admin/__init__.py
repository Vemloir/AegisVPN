"""Admin panel handlers, split by domain into a small package.

The public surface is :data:`router` (assembled from the per-domain
sub-routers) plus a few helpers re-exported for convenience.
"""

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from . import panel, plans, servers, users
from .common import fmt_bytes, is_admin
from .states import AdminStates


class _AdminOnly(Filter):
    """Router-wide guard. Every handler still checks is_admin itself; a future
    handler that forgets the check is then unreachable rather than open."""

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and is_admin(user.id)


# While the panel waits for an id, a price or a number of days, a command is
# still a command: the input handlers skip text starting with "/", and this
# leaves the input step so the next plain message isn't parsed as the awaited
# value either. SkipHandler lets the command reach its own handler.
_commands_leave_input = Router()


@_commands_leave_input.message(StateFilter(AdminStates), F.text.startswith("/"))
async def _command_during_admin_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    raise SkipHandler


router = Router()
router.message.filter(_AdminOnly())
router.callback_query.filter(_AdminOnly())
router.include_router(_commands_leave_input)
router.include_router(panel.router)
router.include_router(servers.router)
router.include_router(plans.router)
router.include_router(users.router)

__all__ = ["AdminStates", "fmt_bytes", "is_admin", "router"]
