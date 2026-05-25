from aiogram import Router

from .admin import router as admin_router
from .payment import router as payment_router
from .user import router as user_router


def setup_routers() -> Router:
    main_router = Router()
    main_router.include_router(admin_router)  # Admin first to catch /admin
    main_router.include_router(payment_router)
    main_router.include_router(user_router)
    return main_router
