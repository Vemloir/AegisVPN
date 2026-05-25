import asyncio
import contextlib
import signal
from datetime import UTC

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import select

from src.core.bootstrap import bootstrap_application
from src.core.config import settings
from src.core.database import async_session_maker
from src.core.logger import setup_logger
from src.handlers import setup_routers
from src.middlewares.identity import IdentitySyncMiddleware
from src.models import Plan, Server
from src.scheduler import setup_scheduler
from src.services import SubscriptionService
from src.services.agent_client import close_session as close_agent_session

logger = setup_logger()
bot_public_url: str | None = settings.bot_public_url.rstrip("/") if settings.bot_public_url else None


async def resolve_bot_public_url(bot: Bot) -> str | None:
    if settings.bot_public_url:
        return settings.bot_public_url.rstrip("/")

    try:
        me = await bot.get_me()
    except Exception as exc:
        logger.warning("Failed to resolve bot public URL: %s", exc)
        return None

    if me.username:
        return f"https://t.me/{me.username}"
    return None


async def configure_bot_commands(bot: Bot) -> None:
    default_commands = [
        BotCommand(command="start", description="main menu"),
        BotCommand(command="help", description="help"),
        BotCommand(command="subscription", description="my subscription"),
        BotCommand(command="settings", description="settings"),
    ]
    await bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    admin_commands = default_commands + [BotCommand(command="admin", description="admin panel")]
    for admin_id in settings.admin_ids:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))


async def get_public_info() -> dict:
    async with async_session_maker() as session:
        plans_result = await session.execute(select(Plan).where(Plan.is_active == True).order_by(Plan.days))
        servers_result = await session.execute(select(Server).where(Server.is_active == True).order_by(Server.name))

        plans = [
            {
                "days": plan.days,
                "stars_price": plan.stars_price,
            }
            for plan in plans_result.scalars().all()
        ]
        servers = [
            {
                "name": server.name,
                "flag": server.flag,
                "host": server.host,
                "port": server.port,
            }
            for server in servers_result.scalars().all()
        ]

    return {
        "title": settings.site_title,
        "description": settings.site_description,
        "base_url": settings.base_url,
        "telegram_mode": settings.telegram_mode,
        "servers": servers,
        "plans": plans,
    }


async def index_handler(request: web.Request) -> web.Response:
    return web.Response(status=404, text="Not found")


async def info_handler(request: web.Request) -> web.Response:
    return web.Response(status=404, text="Not found")


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "mode": settings.telegram_mode})


async def sub_handler(request: web.Request) -> web.Response:
    return await subscription_response(request, SubscriptionService.SAFE_PROFILE)


async def sub_safe_handler(request: web.Request) -> web.Response:
    return await subscription_response(request, SubscriptionService.SAFE_PROFILE)


async def sub_fast_handler(request: web.Request) -> web.Response:
    return await subscription_response(request, SubscriptionService.FAST_PROFILE)


async def subscription_response(request: web.Request, profile: str) -> web.Response:
    sub_token = request.match_info.get("token")
    if not sub_token:
        return web.Response(status=400, text="Token missing")

    async with async_session_maker() as session:
        sub = await SubscriptionService.get_subscription_by_token(session, sub_token)
        b64_content = await SubscriptionService.get_subscription_vless_links(session, sub_token, profile=profile)

    if not sub or not b64_content:
        return web.Response(status=404, text="Subscription not found or inactive")

    response = web.Response(text=b64_content, content_type="text/plain")
    title = (
        f"{settings.subscription_title} Fast"
        if profile == SubscriptionService.FAST_PROFILE
        else settings.subscription_title
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{title}"'
    response.headers["Profile-Title"] = title
    # Real per-client byte counters, summed across all of the user's nodes
    # by the poll_traffic scheduler task. `total=0` means "no quota" — it is
    # the standard unlimited marker, and clients (Happ, v2rayN, …) only draw
    # the usage capsule when the `total` key is present, rendering it as ∞.
    upload = max(int(getattr(sub, "traffic_up_bytes", 0) or 0), 0)
    download = max(int(getattr(sub, "traffic_down_bytes", 0) or 0), 0)
    userinfo_parts = [f"upload={upload}", f"download={download}", "total=0"]
    if not SubscriptionService.is_lifetime_subscription(sub):
        userinfo_parts.append(f"expire={int(sub.expires_at.replace(tzinfo=UTC).timestamp())}")
    response.headers["Subscription-Userinfo"] = "; ".join(userinfo_parts)
    if settings.subscription_update_interval_hours > 0:
        response.headers["Profile-Update-Interval"] = str(settings.subscription_update_interval_hours)
    if bot_public_url:
        response.headers["Support-Url"] = bot_public_url
        response.headers["Profile-Web-Page-Url"] = bot_public_url
    return response


def create_bot() -> Bot:
    return Bot(token=settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode="HTML"))


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # Keep stored usernames fresh on every interaction so admin lookup by
    # @username works after a user changes it.
    identity = IdentitySyncMiddleware()
    dp.message.middleware(identity)
    dp.callback_query.middleware(identity)
    dp.include_router(setup_routers())
    return dp


def create_web_app(bot: Bot | None = None, dp: Dispatcher | None = None) -> web.Application:
    app = web.Application()

    if settings.telegram_mode == "webhook" and bot is not None and dp is not None:
        webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_requests_handler.register(app, path=settings.webhook_path)
        setup_application(app, dp, bot=bot)

    app.router.add_get("/", index_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/info.json", info_handler)
    app.router.add_get("/sub/{token}", sub_handler)
    app.router.add_get("/sub-safe/{token}", sub_safe_handler)
    app.router.add_get("/sub-fast/{token}", sub_fast_handler)
    return app


async def start_http_server(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.webapp_host, port=settings.webapp_port)
    await site.start()
    logger.info(f"HTTP server started on {settings.webapp_host}:{settings.webapp_port}")
    return runner


async def wait_for_shutdown() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()


async def run_polling_mode(bot: Bot, dp: Dispatcher) -> None:
    scheduler = setup_scheduler(bot)
    scheduler.start()

    app = create_web_app()
    runner = await start_http_server(app)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Telegram mode: polling")
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await close_agent_session()
        await bot.session.close()


async def run_webhook_mode(bot: Bot, dp: Dispatcher) -> None:
    scheduler = setup_scheduler(bot)
    scheduler.start()

    app = create_web_app(bot=bot, dp=dp)
    runner = await start_http_server(app)
    webhook_url = f"{settings.base_url}{settings.webhook_path}"

    try:
        await bot.set_webhook(webhook_url)
        logger.info(f"Telegram mode: webhook ({webhook_url})")
        await wait_for_shutdown()
    finally:
        scheduler.shutdown(wait=False)
        with contextlib.suppress(Exception):
            await bot.delete_webhook(drop_pending_updates=False)
        await runner.cleanup()
        await close_agent_session()
        await bot.session.close()


async def run() -> None:
    global bot_public_url

    await bootstrap_application()

    bot = create_bot()
    bot_public_url = await resolve_bot_public_url(bot)
    await configure_bot_commands(bot)
    dp = create_dispatcher()

    if settings.telegram_mode.lower() == "polling":
        await run_polling_mode(bot, dp)
        return

    await run_webhook_mode(bot, dp)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
