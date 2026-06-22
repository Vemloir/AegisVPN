"""End-user handlers, split by domain into a small package.

Public surface is :data:`router`, assembled from the per-domain sub-routers.
"""

from aiogram import Router

from . import devices, privacy, settings, start, subscription, terms

router = Router()
router.include_router(terms.router)  # accept callback must be reachable through the gate
router.include_router(start.router)
router.include_router(privacy.router)
router.include_router(subscription.router)
router.include_router(settings.router)
router.include_router(devices.router)

__all__ = ["router"]
