"""Admin panel handlers, split by domain into a small package.

The public surface is :data:`router` (assembled from the per-domain
sub-routers) plus a few helpers re-exported for convenience.
"""

from aiogram import Router

from . import panel, plans, servers, users
from .common import fmt_bytes, is_admin
from .states import AdminStates

router = Router()
router.include_router(panel.router)
router.include_router(servers.router)
router.include_router(plans.router)
router.include_router(users.router)

__all__ = ["AdminStates", "fmt_bytes", "is_admin", "router"]
