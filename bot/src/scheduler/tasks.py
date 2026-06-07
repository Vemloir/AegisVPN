import asyncio
import json
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from src.core.config import settings
from src.core.database import async_session_maker
from src.core.logger import logger
from src.models import Device, Server, Subscription, SubscriptionServer, User
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


# Per-(device_id, server_id) last seen counters for delta accounting.
# In-memory only; resets on bot restart (recovers within one poll cycle).
_device_last_stats: dict[tuple[int, int], tuple[int, int]] = {}

# Fallback online detection for old agents (no /online-emails): last raw counters
# keyed by (device_id | -sub_id, server_id). In-memory only.
_online_fallback_last: dict[tuple[int, int], tuple[int, int]] = {}


async def poll_traffic():
    """Pull per-client byte counters from every node and accumulate them.

    Xray's counters are cumulative since its last start and reset to 0 on
    restart, so we store the last raw value per (subscription, node) and add
    only the positive delta. A negative delta means Xray restarted — then the
    current value itself is the delta.
    """
    logger.info("Running task: poll_traffic")
    now = datetime.now(UTC).replace(tzinfo=None)
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
                if delta_up < 0:
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

            # --- per-device traffic bytes (from /stats device emails) -----------
            for email, cur in stats.items():
                device_id = _parse_dev_id(email)
                if device_id is None:
                    continue
                cur_up = int(cur.get("uplink", 0) or 0)
                cur_down = int(cur.get("downlink", 0) or 0)
                key = (device_id, server.id)
                last_up, last_down = _device_last_stats.get(key, (0, 0))
                delta_up = cur_up - last_up if cur_up >= last_up else cur_up
                delta_down = cur_down - last_down if cur_down >= last_down else cur_down
                _device_last_stats[key] = (cur_up, cur_down)
                if delta_up or delta_down:
                    device = (await session.execute(
                        select(Device).where(Device.id == device_id, Device.is_active == True)  # noqa: E712
                    )).scalar_one_or_none()
                    if device:
                        device.traffic_up_bytes = (device.traffic_up_bytes or 0) + delta_up
                        device.traffic_down_bytes = (device.traffic_down_bytes or 0) + delta_down
                        changed = True

            # --- authoritative online state (from /online-emails) --------------
            # Xray reports exactly which emails have a live session right now, so we
            # mark last_active/last_server from that, not from flaky traffic deltas.
            # Old agents without the endpoint return None → fall back to the delta
            # heuristic (traffic moved since the last poll ⇒ treat as online).
            online_emails = await client.get_online_emails()
            if online_emails is not None:
                if await _mark_devices_online(session, server.id, online_emails, now):
                    changed = True
            else:
                if await _mark_devices_online_fallback(session, server.id, stats, now):
                    changed = True

        if changed:
            await session.commit()


def _parse_dev_id(email: str) -> int | None:
    """Extract the device id from a per-device email `user_X_sub_Y_dev_Z`."""
    if "_dev_" not in email:
        return None
    try:
        return int(email.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return None


async def _mark_devices_online(session, server_id: int, online_emails: set[str], now: datetime) -> bool:
    """Mark devices connected to `server_id` from the node's live online emails.

    A connection is attributed to a device ONLY by its own per-device email
    `user_X_sub_Y_dev_Z`. The shared legacy email (a client still on the pre-device
    UUID) is deliberately ignored: it can't be tied to a specific device, and
    guessing produced wrong results (one device's session shown under another).
    """
    changed = False
    for email in online_emails:
        device_id = _parse_dev_id(email)
        if device_id is None:
            continue
        device = (await session.execute(
            select(Device).where(Device.id == device_id, Device.is_active == True)  # noqa: E712
        )).scalar_one_or_none()
        if device and not device.is_suspended:
            device.last_active_at = now
            device.last_server_id = server_id
            changed = True
    return changed


async def _mark_devices_online_fallback(session, server_id: int, stats: dict, now: datetime) -> bool:
    """Online detection for nodes whose agent lacks /online-emails (during rollout).

    Per-device emails only — a device is online if its own counter moved since the
    last poll. Shared legacy emails are ignored (can't be tied to one device).
    """
    changed = False
    for email, cur in stats.items():
        device_id = _parse_dev_id(email)
        if device_id is None:
            continue
        cur_up = int(cur.get("uplink", 0) or 0)
        cur_down = int(cur.get("downlink", 0) or 0)
        key = (device_id, server_id)
        last_up, last_down = _online_fallback_last.get(key, (0, 0))
        moved = cur_up > last_up or cur_down > last_down
        _online_fallback_last[key] = (cur_up, cur_down)
        if not moved:
            continue
        device = (await session.execute(
            select(Device).where(Device.id == device_id, Device.is_active == True)  # noqa: E712
        )).scalar_one_or_none()
        if device and not device.is_suspended:
            device.last_active_at = now
            device.last_server_id = server_id
            changed = True
    return changed


def _make_full_backup() -> Path | None:
    """Create AegisVPN-BACKUP-DD.MM.YYYY-HH:MM.tar.gz containing:
      - aegis.db  (consistent SQLite snapshot via online backup API)
      - agent.env (Finland Reality keys + agent token)
      - bot.env   (bot token, admin IDs and other critical settings)
    Returns the archive path, or None if the DB is not SQLite."""
    if not settings.db_url.startswith("sqlite+aiosqlite"):
        return None

    src = settings.sqlite_path
    if not Path(src).exists():
        logger.warning("backup: db file %s missing", src)
        return None

    out_dir = Path("/data/backups")
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%d.%m.%Y-%H:%M")
    archive = out_dir / f"AegisVPN-BACKUP-{stamp}.tar.gz"
    db_snap = out_dir / "_aegis_snap.db"
    bot_env_tmp = out_dir / "_bot.env"

    # 1. Consistent DB snapshot
    dst = sqlite3.connect(db_snap)
    try:
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as live:
            live.backup(dst)
    finally:
        dst.close()

    # 2. Reconstruct bot.env from live settings
    lines = [
        f"BOT_TOKEN={settings.bot_token.get_secret_value()}",
        f"ADMIN_IDS={json.dumps(settings.admin_ids)}",
    ]
    for key, val in [
        ("BOT_DOMAIN", settings.bot_domain),
        ("PUBLIC_BASE_URL", settings.public_base_url),
        ("SUBSCRIPTION_PUBLIC_BASE_URL", settings.subscription_public_base_url),
        ("BOT_PUBLIC_URL", settings.bot_public_url),
        ("TELEGRAM_MODE", settings.telegram_mode),
        ("WEBAPP_PORT", settings.webapp_port),
        ("SITE_TITLE", settings.site_title),
        ("SUBSCRIPTION_TITLE", settings.subscription_title),
    ]:
        if val is not None:
            lines.append(f"{key}={val}")
    bot_env_tmp.write_text("\n".join(lines) + "\n")

    # 3. Pack everything
    agent_env = Path(settings.bootstrap_server_agent_env)  # /vpn-data/agent.env
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(db_snap, arcname="aegis.db")
        tar.add(bot_env_tmp, arcname="bot.env")
        if agent_env.exists():
            tar.add(agent_env, arcname="agent.env")
        else:
            logger.warning("backup: agent.env not found at %s", agent_env)

    db_snap.unlink(missing_ok=True)
    bot_env_tmp.unlink(missing_ok=True)

    # Rotate local copies
    backups = sorted(out_dir.glob("AegisVPN-BACKUP-*.tar.gz"))
    for old in backups[: max(0, len(backups) - settings.backup_keep)]:
        old.unlink(missing_ok=True)

    return archive


# message_id of the last backup document sent to each admin (for editing in place)
_backup_msg_ids: dict[int, int] = {}


async def send_backup_to_admins(bot: Bot):
    """Hourly: send (or silently edit in place) a backup document to every admin."""
    if not settings.backup_enabled:
        return
    logger.info("Running task: send_backup_to_admins")
    try:
        gz = await asyncio.to_thread(_make_full_backup)
    except Exception as exc:
        logger.error("backup failed: %s", exc)
        return
    if gz is None:
        return

    data = gz.read_bytes()
    caption = f"Бекапп БД {datetime.now(UTC):%Y-%m-%d %H:%M} UTC ({len(data) // 1024} KiB)"

    for admin_id in settings.admin_ids:
        try:
            existing_id = _backup_msg_ids.get(admin_id)
            if existing_id:
                try:
                    await bot.edit_message_media(
                        chat_id=admin_id,
                        message_id=existing_id,
                        media=InputMediaDocument(
                            media=BufferedInputFile(data, filename=gz.name),
                            caption=caption,
                        ),
                    )
                    continue
                except Exception:
                    pass  # message too old or deleted — fall through to send new
            msg = await bot.send_document(
                admin_id,
                BufferedInputFile(data, filename=gz.name),
                caption=caption,
            )
            _backup_msg_ids[admin_id] = msg.message_id
        except Exception as exc:
            logger.error("backup send to admin %s failed: %s", admin_id, exc)


async def backup_database(bot: Bot):
    """Daily: write a rotated local DB snapshot to /data/backups.

    The snapshot is kept on disk (rotation: backup_keep); admins pull it on
    demand via the "download DB" button. Only a failure is reported to admins.
    """
    if not settings.backup_enabled:
        return
    logger.info("Running task: backup_database")
    try:
        gz = await asyncio.to_thread(_make_full_backup)
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
HEALTH_FAIL_THRESHOLD = 2  # 1-min interval + immediate retry → alert in ~2 min


async def _alert_admins(bot: Bot, text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.error("alert to %s failed: %s", admin_id, exc)


async def _probe_xray_port(host: str, port: int, timeout: float = 4.0) -> bool:
    """True iff a TCP connection to the node's VLESS port completes."""
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


async def _check_one(server: Server) -> tuple[bool, str, int | None]:
    """Return (ok, reason, clients) for a single server."""
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

    return agent_ok and xray_ok, reason, clients


async def check_servers_health(bot: Bot):
    logger.info("Running task: check_servers_health")
    async with async_session_maker() as session:
        result = await session.execute(select(Server).where(Server.is_active == True))
        servers = result.scalars().all()

    for server in servers:
        ok, reason, clients = await _check_one(server)

        # On first failure do an immediate retry after a short pause to avoid
        # alerting on transient blips (brief network hiccup, sshd rate-limit).
        if not ok:
            await asyncio.sleep(3)
            ok, reason, clients = await _check_one(server)

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
