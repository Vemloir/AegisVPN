import asyncio
import contextlib
import signal
from datetime import UTC

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import select

from src.core.bootstrap import bootstrap_application
from src.core.config import settings
from src.core.database import async_session_maker
from src.core.leader import run_leader_worker
from src.core.logger import setup_logger
from src.handlers import setup_routers
from src.middlewares.identity import IdentitySyncMiddleware
from src.middlewares.terms_gate import TermsGateMiddleware
from src.models import Payment, Plan, Server
from src.scheduler import setup_scheduler
from src.services import SubscriptionService, confirm_platega_payment
from src.services.agent_client import close_session as close_agent_session
from src.services.platega_client import verify_callback_headers

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
    # NOTE: /help still WORKS when typed; it is just not listed in the menu.
    default_commands = [
        BotCommand(command="start", description="main menu"),
        BotCommand(command="subscription", description="my subscription"),
        BotCommand(command="settings", description="settings"),
        BotCommand(command="info", description="about, documents, news"),
    ]
    await bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())
    # An older build set an all_private_chats list (with /help, no /info); that
    # narrower scope overrides the default for regular users. Clear it so the
    # default scope above governs.
    await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())

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


def subscription_metadata_headers(sub, profile: str) -> dict[str, str]:
    """Standard subscription metadata understood by Happ/v2ray clients."""
    title = (
        f"{settings.subscription_title} Fast"
        if profile == SubscriptionService.FAST_PROFILE
        else settings.subscription_title
    )
    upload = max(int(getattr(sub, "traffic_up_bytes", 0) or 0), 0)
    download = max(int(getattr(sub, "traffic_down_bytes", 0) or 0), 0)
    userinfo_parts = [f"upload={upload}", f"download={download}", "total=0"]
    if not SubscriptionService.is_lifetime_subscription(sub):
        userinfo_parts.append(f"expire={int(sub.expires_at.replace(tzinfo=UTC).timestamp())}")

    headers = {
        "Profile-Title": title,
        "Subscription-Userinfo": "; ".join(userinfo_parts),
        "Support-Url": settings.support_public_url.rstrip("/"),
        "Profile-Web-Page-Url": settings.site_public_url.rstrip("/"),
        "Subscription-Ping-Onopen-Enabled": "1",
    }
    if settings.subscription_update_interval_hours > 0:
        headers["Profile-Update-Interval"] = str(settings.subscription_update_interval_hours)
    return headers


def _client_ip(request: web.Request) -> str | None:
    """Real client IP behind the Caddy reverse proxy (first X-Forwarded-For hop)."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote


# Clients that consume a full xray-JSON config array (routing/DNS baked in)
# instead of a base64 vless link list. Matched case-insensitively as a substring
# of the User-Agent. Everything else (Clash, sing-box, browsers, curl) keeps the
# link list, so this is additive and non-breaking for non-xray clients.
_XRAY_JSON_CLIENTS = (
    "happ",
    "v2raytun",
    "v2rayng",
    "v2rayn",
    "nekobox",
    "nekoray",
    "streisand",
    "foxray",
    "ua varmlen",
)


def client_wants_xray_json(user_agent: str) -> bool:
    return any(client in user_agent.lower() for client in _XRAY_JSON_CLIENTS)


async def subscription_response(request: web.Request, profile: str) -> web.Response:
    sub_token = request.match_info.get("token")
    if not sub_token:
        return web.Response(status=400, text="Token missing")

    ua = request.headers.get("User-Agent", "").strip()
    client_ip = _client_ip(request)
    # xray clients (Happ, v2rayTun, …) get a full JSON config array with the
    # routing/DNS baked in; everyone else keeps the base64 link list.
    wants_xray_json = client_wants_xray_json(ua)

    async with async_session_maker() as session:
        sub = await SubscriptionService.get_subscription_by_token(session, sub_token)

        device_uuid: str | None = None
        if sub and ua:
            device = await SubscriptionService.get_or_create_device(session, sub, ua, client_ip)
            await session.commit()
            if not device.is_suspended:
                device_uuid = device.uuid

        if wants_xray_json:
            # The builder may downgrade to a base64 link list when the sub
            # contains a Hysteria2 location (xray-core can't run hysteria2://),
            # so the actual response kind comes back with the body.
            kind, body = await SubscriptionService.build_xray_json_subscription(
                session, sub_token, profile=profile, device_uuid=device_uuid
            )
            wants_xray_json = kind == "json"
        else:
            body = await SubscriptionService.get_subscription_vless_links(
                session, sub_token, profile=profile, device_uuid=device_uuid
            )

    if not sub or not body:
        return web.Response(status=404, text="Subscription not found or inactive")

    response = web.Response(text=body, content_type="application/json" if wants_xray_json else "text/plain")
    metadata_headers = subscription_metadata_headers(sub, profile)
    title = metadata_headers["Profile-Title"]
    response.headers["Content-Disposition"] = f'attachment; filename="{title}"'
    response.headers.update(metadata_headers)
    # Auto-ping all locations the moment the subscription opens, so users see
    # fresh latencies instead of "н/д" until they tap each one.
    #
    # NOTE: do NOT advertise Mux here. Our inbounds use flow=xtls-rprx-vision,
    # and VLESS mux is incompatible with the vision flow — turning it on (as we
    # briefly did) makes clients multiplex over a vision connection and traffic
    # silently dies ("connects but nothing loads"). Vision already coalesces
    # sub-streams itself, so mux buys nothing here anyway.
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
    # Mandatory legal-acceptance gate: blocks everything (except the accept tap)
    # until the user accepts the current Privacy Policy + Terms of Service.
    terms_gate = TermsGateMiddleware()
    dp.message.middleware(terms_gate)
    dp.callback_query.middleware(terms_gate)
    dp.include_router(setup_routers())
    return dp


async def platega_callback_handler(request: web.Request) -> web.Response:
    """Platega POSTs here on a transaction status change. Auth is the same
    X-MerchantId / X-Secret headers echoed back to us (no HMAC). On CONFIRMED we
    activate the subscription (idempotent by transaction id); we always ack with
    200 so Platega stops retrying."""
    if not settings.platega_enabled:
        return web.Response(status=503)
    if not verify_callback_headers(request.headers.get("X-MerchantId"), request.headers.get("X-Secret")):
        return web.Response(status=403)

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=400)

    tx_id = data.get("id")
    status = (data.get("status") or "").upper()
    if not tx_id:
        return web.Response(status=400)

    async with async_session_maker() as session:
        payment = (
            await session.execute(select(Payment).where(Payment.tg_payment_id == f"platega_{tx_id}"))
        ).scalar_one_or_none()
        if payment is None:
            logger.warning(f"Platega callback for unknown tx {tx_id} (status {status})")
            return web.Response(status=200)

        # Defence-in-depth: a fixed-amount СБП QR can't be underpaid, but flag any
        # mismatch between the callback amount and what we billed.
        cb_amount = data.get("amount")
        if cb_amount is not None and payment.rub_amount is not None and int(cb_amount) != payment.rub_amount:
            logger.warning(f"Platega callback amount mismatch tx {tx_id}: got {cb_amount}, billed {payment.rub_amount}")

        if status == "CONFIRMED":
            await confirm_platega_payment(session, payment, bot=request.app.get("bot"))
        elif status == "CHARGEBACKED":
            # A chargeback usually lands AFTER we already confirmed and granted
            # access, so the money is reversed but the user still has the VPN. We
            # can't silently drop this: log it and alert every admin so the
            # operator can revoke manually. Don't clobber the "confirmed"
            # idempotency status; only set it if we hadn't confirmed yet.
            logger.error(
                f"Platega CHARGEBACK tx {tx_id} (payment {payment.id}, user {payment.user_id}, "
                f"{payment.rub_amount} RUB, prior status {payment.status})"
            )
            bot = request.app.get("bot")
            if bot is not None:
                alert = (
                    f"Chargeback Platega: платёж {tx_id}, пользователь {payment.user_id}, "
                    f"сумма {payment.rub_amount} р. Проверьте и при необходимости отзовите подписку."
                )
                for admin_id in settings.admin_ids:
                    with contextlib.suppress(Exception):
                        await bot.send_message(admin_id, alert)
            if payment.status != "confirmed":
                payment.status = "chargeback"
                await session.commit()
        elif status == "CANCELED":
            if payment.status != "confirmed":
                payment.status = "canceled"
                await session.commit()
    return web.Response(status=200)


def create_web_app(bot: Bot | None = None, dp: Dispatcher | None = None) -> web.Application:
    app = web.Application()
    # Stash the bot so non-Telegram routes (the Platega callback) can DM users,
    # in polling mode too where SimpleRequestHandler isn't wired up.
    app["bot"] = bot

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
    app.router.add_post(settings.platega_callback_path, platega_callback_handler)
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

    app = create_web_app(bot=bot)
    runner = await start_http_server(app)

    try:

        async def poll() -> None:
            await bot.delete_webhook(drop_pending_updates=False)
            logger.info("Telegram mode: polling (leader)")
            await dp.start_polling(bot)

        await run_leader_worker("telegram-main-polling", poll)
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
