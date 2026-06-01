import asyncio
import gzip
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from src.core.config import settings
from src.core.database import async_session_maker
from src.core.logger import logger
from src.models import Server, Subscription, SubscriptionServer, User
from src.services import AgentClient, SubscriptionService, t


def renewal_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(language, "renew_vpn"), callback_data="buy_plan")]]
    )


async def check_expired_subscriptions(bot: Bot):
    logger.info("Running task: check_expired_subscriptions")
    async with async_session_maker() as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        result = await session.execute(
            select(Subscription).where(
                Subscription.expires_at <= now,
                Subscription.is_active == True,
            )
        )
        expired_subs = result.scalars().all()

        for sub in expired_subs:
            sub.is_active = False
            await SubscriptionService.remove_subscription_from_servers(session, sub)

            user_result = await session.execute(select(User).where(User.id == sub.user_id))
            user = user_result.scalar_one_or_none()
            if not user or user.is_banned:
                continue

            try:
                await bot.send_message(
                    user.tg_id,
                    t(user.language, "expired_notice"),
                    reply_markup=renewal_keyboard(user.language),
                )
            except Exception as exc:
                logger.error(f"Failed to send expiration notice to {user.tg_id}: {exc}")

        await session.commit()


async def remind_expiring_subscriptions(bot: Bot):
    logger.info("Running task: remind_expiring_subscriptions")
    async with async_session_maker() as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        reminder_windows = [
            (timedelta(days=3), timedelta(hours=1), "remind_3d"),
            (timedelta(days=1), timedelta(hours=1), "remind_1d"),
        ]

        for target_delta, window, text_key in reminder_windows:
            end_time = now + target_delta
            start_time = end_time - window
            result = await session.execute(
                select(Subscription).where(
                    Subscription.expires_at > start_time,
                    Subscription.expires_at <= end_time,
                    Subscription.is_active == True,
                )
            )
            subscriptions = result.scalars().all()

            for sub in subscriptions:
                user_result = await session.execute(select(User).where(User.id == sub.user_id))
                user = user_result.scalar_one_or_none()
                if not user or user.is_banned:
                    continue

                try:
                    await bot.send_message(
                        user.tg_id,
                        t(user.language, text_key),
                        reply_markup=renewal_keyboard(user.language),
                    )
                except Exception as exc:
                    logger.error(f"Failed to send renewal reminder to {user.tg_id}: {exc}")


async def retry_unsynced_servers():
    logger.info("Running task: retry_unsynced_servers")
    async with async_session_maker() as session:
        result = await session.execute(
            select(SubscriptionServer)
            .options(
                joinedload(SubscriptionServer.subscription),
                joinedload(SubscriptionServer.server),
            )
            .where(
                SubscriptionServer.is_synced == False,
                SubscriptionServer.subscription.has(Subscription.is_active == True),
                SubscriptionServer.server.has(Server.is_active == True),
            )
        )
        unsynced_links = result.scalars().all()

        for sub_server in unsynced_links:
            server = sub_server.server
            sub = sub_server.subscription
            client = AgentClient(server.agent_url, server.agent_token)
            try:
                email = f"user_{sub.user_id}_sub_{sub.id}"
                success = await client.add_client(sub.client_uuid, email)
                if success:
                    sub_server.is_synced = True
                    logger.info(f"Successfully synced {sub.client_uuid} to server {server.id}")
            except Exception as exc:
                logger.error(f"Retry sync failed for {sub.client_uuid} to server {server.id}: {exc}")

        await session.commit()


async def poll_traffic():
    """Pull per-client byte counters from every node and accumulate them.

    Xray's counters are cumulative since its last start and reset to 0 on
    restart, so we store the last raw value per (subscription, node) and add
    only the positive delta. A negative delta means Xray restarted — then the
    current value itself is the delta.
    """
    logger.info("Running task: poll_traffic")
    async with async_session_maker() as session:
        servers = (await session.execute(select(Server).where(Server.is_active == True))).scalars().all()

        changed = False
        for server in servers:
            client = AgentClient(server.agent_url, server.agent_token)
            try:
                stats = await client.get_stats()
            except Exception as exc:
                logger.warning("traffic poll: server %s stats failed: %s", server.id, exc)
                continue
            if not stats:
                continue

            links = (
                (
                    await session.execute(
                        select(SubscriptionServer)
                        .options(joinedload(SubscriptionServer.subscription))
                        .where(SubscriptionServer.server_id == server.id)
                    )
                )
                .scalars()
                .all()
            )

            for link in links:
                sub = link.subscription
                if sub is None:
                    continue
                email = f"user_{sub.user_id}_sub_{sub.id}"
                cur = stats.get(email)
                if not cur:
                    continue

                cur_up = int(cur.get("uplink", 0) or 0)
                cur_down = int(cur.get("downlink", 0) or 0)

                delta_up = cur_up - (link.traffic_last_up or 0)
                if delta_up < 0:  # Xray restarted, counter reset to 0
                    delta_up = cur_up
                delta_down = cur_down - (link.traffic_last_down or 0)
                if delta_down < 0:
                    delta_down = cur_down

                if delta_up:
                    sub.traffic_up_bytes = (sub.traffic_up_bytes or 0) + delta_up
                    link.traffic_up_bytes = (link.traffic_up_bytes or 0) + delta_up
                    changed = True
                if delta_down:
                    sub.traffic_down_bytes = (sub.traffic_down_bytes or 0) + delta_down
                    link.traffic_down_bytes = (link.traffic_down_bytes or 0) + delta_down
                    changed = True
                if link.traffic_last_up != cur_up or link.traffic_last_down != cur_down:
                    link.traffic_last_up = cur_up
                    link.traffic_last_down = cur_down
                    changed = True

        if changed:
            await session.commit()


def _make_sqlite_backup() -> Path | None:
    """Consistent gzip snapshot of the SQLite DB via the online backup API
    (does not block or corrupt while the bot writes). Returns the .gz path."""
    if not settings.db_url.startswith("sqlite+aiosqlite"):
        return None  # postgres handled separately if ever used
    src = settings.sqlite_path
    if not Path(src).exists():
        logger.warning("backup: db file %s missing", src)
        return None
    out_dir = Path("/data/backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    snap = out_dir / f"aegis-{stamp}.db"
    gz = out_dir / f"aegis-{stamp}.db.gz"

    # consistent snapshot
    dst = sqlite3.connect(snap)
    try:
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as live:
            live.backup(dst)
    finally:
        dst.close()

    with open(snap, "rb") as f_in, gzip.open(gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    snap.unlink(missing_ok=True)

    # rotate local copies
    backups = sorted(out_dir.glob("aegis-*.db.gz"))
    for old in backups[: max(0, len(backups) - settings.backup_keep)]:
        old.unlink(missing_ok=True)
    return gz


async def backup_database(bot: Bot):
    """Daily: write a rotated local DB snapshot to /data/backups.

    The snapshot is kept on disk (rotation: backup_keep); admins pull it on
    demand via the "download DB" button. Only a failure is reported to admins.
    """
    if not settings.backup_enabled:
        return
    logger.info("Running task: backup_database")
    try:
        gz = await asyncio.to_thread(_make_sqlite_backup)
    except Exception as exc:
        logger.error("backup failed: %s", exc)
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, f"DB backup FAILED: {exc}")
            except Exception:
                pass
        return
    if gz is not None:
        logger.info("DB backup written: %s", gz)


# Per-server consecutive-failure counters. A node is only declared DOWN after
# HEALTH_FAIL_THRESHOLD failures in a row, so a single transient blip (sshd
# rate-limit, a momentary network hiccup) doesn't page admins. Servers we've
# already alerted on live in `_down_servers`, so the message fires once on the
# down edge and once on recovery — not every cycle.
_down_servers: set[int] = set()
_fail_counts: dict[int, int] = {}
HEALTH_FAIL_THRESHOLD = 2  # at the 5-min job interval → ~10 min before alerting


async def _alert_admins(bot: Bot, text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.error("alert to %s failed: %s", admin_id, exc)


async def _probe_xray_port(host: str, port: int, timeout: float = 4.0) -> bool:
    """True iff a TCP connection to the node's VLESS port completes. The agent
    API (:8444) answering doesn't prove xray (the port users connect to) is
    actually serving — so a closed VLESS port means the node is down for users
    even while the agent is alive. We check both."""
    try:
        fut = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def check_servers_health(bot: Bot):
    logger.info("Running task: check_servers_health")
    async with async_session_maker() as session:
        result = await session.execute(select(Server).where(Server.is_active == True))
        servers = result.scalars().all()

    for server in servers:
        client = AgentClient(server.agent_url, server.agent_token)
        agent_ok = False
        clients = None
        reason = ""
        try:
            health = await client.get_health()
            agent_ok = health.get("status") == "ok"
            clients = health.get("clients")
            if not agent_ok:
                reason = f"agent status={health.get('status')!r}"
        except Exception as exc:
            reason = f"agent unreachable ({type(exc).__name__})"

        xray_ok = await _probe_xray_port(server.host, server.port)
        if not xray_ok:
            reason = (reason + "; " if reason else "") + f"xray port {server.port} closed"
        ok = agent_ok and xray_ok

        was_down = server.id in _down_servers
        if ok:
            _fail_counts.pop(server.id, None)
            if was_down:
                _down_servers.discard(server.id)
                logger.info("Server %s (%s) recovered", server.id, server.name)
                await _alert_admins(
                    bot,
                    f"Нода снова в строю: {server.name}\n"
                    f"{server.host}:{server.port}",
                )
            continue

        # Failing this cycle — bump the streak, alert only once we cross the
        # threshold (and haven't alerted already).
        fails = _fail_counts.get(server.id, 0) + 1
        _fail_counts[server.id] = fails
        logger.warning(
            "health: %s (%s) failing %d/%d — %s",
            server.name, server.host, fails, HEALTH_FAIL_THRESHOLD, reason,
        )
        if fails >= HEALTH_FAIL_THRESHOLD and not was_down:
            _down_servers.add(server.id)
            clients_line = f"\nКлиентов было: {clients}" if clients is not None else ""
            await _alert_admins(
                bot,
                f"Нода НЕ отвечает: {server.name}\n"
                f"{server.host}:{server.port}\n"
                f"Причина: {reason}\n"
                f"Неудачных проверок подряд: {fails}{clients_line}",
            )
