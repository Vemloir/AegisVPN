"""End-user handlers, split by domain into a small package.

Public surface is :data:`router`, assembled from the per-domain sub-routers.
"""

from aiogram import Router

from . import devices, privacy, settings, start, subscription

router = Router()
router.include_router(start.router)
router.include_router(privacy.router)
router.include_router(subscription.router)
router.include_router(settings.router)
router.include_router(devices.router)

__all__ = ["router"]
