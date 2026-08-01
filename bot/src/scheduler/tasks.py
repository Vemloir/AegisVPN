import asyncio
import io
import json
import os
import re
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from sqlalchemy import select, update
from sqlalchemy.orm import joinedload

from src.core.config import settings
from src.core.database import async_session_maker
from src.core.logger import logger
from src.models import Device, NodeTelemetry, Payment, Server, Subscription, SubscriptionServer, User
from src.services import AgentClient, NodeControlService, confirm_platega_payment, t
from src.services.backup_archive import BackupConfigurationError, encrypt_backup
from src.services.platega_client import PlategaError, get_transaction_status

_TRAFFIC_EMAIL_RE = re.compile(r"^user_(\d+)_sub_(\d+)(?:_dev_(\d+))?$")
_SCHEDULER_NETWORK_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class _UnsyncedJob:
    subscription_id: int
    server_id: int
    client_uuid: str
    user_id: int
    control_mode: str
    agent_url: str
    agent_token: str


@dataclass(frozen=True, slots=True)
class _TrafficSource:
    server_id: int
    control_mode: str
    agent_url: str
    agent_token: str
    telemetry_stats: dict[str, dict]


@dataclass(frozen=True, slots=True)
class _ExpiredSubscriptionJob:
    subscription_id: int
    user_tg_id: int | None
    user_language: str
    user_banned: bool
    client_uuids: tuple[str, ...]
    push_targets: tuple[tuple[int, str, str], ...]


def _index_stats_by_subscription(
    stats: dict[str, dict],
) -> dict[tuple[int, int], list[tuple[str, dict]]]:
    """Group Xray counters by subscription in one pass over the payload."""
    indexed: dict[tuple[int, int], list[tuple[str, dict]]] = {}
    for email, counters in stats.items():
        if not isinstance(email, str) or not isinstance(counters, dict):
            continue
        match = _TRAFFIC_EMAIL_RE.fullmatch(email)
        if match is None:
            continue
        key = (int(match.group(1)), int(match.group(2)))
        indexed.setdefault(key, []).append((email, counters))
    return indexed


def renewal_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(language, "renew_vpn"), callback_data="buy_plan")]]
    )


async def reconcile_pending_platega(bot: Bot):
    """Backstop for a missed Platega callback. The callback (with its 3 retries) is
    the primary path; this sweeps recent still-pending payments, asks Platega for
    the real status, and confirms any that were paid but whose webhook never landed
    (or lands while the bot was down). Idempotent — confirm_platega_payment's atomic
    claim means a payment already granted by the callback is a no-op here."""
    if not settings.platega_enabled:
        return

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=6)
    async with async_session_maker() as session:
        pending = (
            (
                await session.execute(
                    select(Payment).where(
                        Payment.provider == "platega",
                        Payment.status == "pending",
                        Payment.created_at >= cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
    if not pending:
        return

    for row in pending:
        tx_id = row.tg_payment_id.removeprefix("platega_")
        try:
            resp = await get_transaction_status(tx_id)
        except PlategaError as exc:
            logger.warning(f"reconcile_pending_platega: status check failed for {tx_id}: {exc}")
            continue
        status = (resp.get("status") or "").upper()
        if status not in ("CONFIRMED", "CANCELED", "CHARGEBACKED"):
            continue  # still PENDING — leave it for the next sweep

        async with async_session_maker() as session:
            payment = await session.get(Payment, row.id)
            if payment is None or payment.status != "pending":
                continue
            if status == "CONFIRMED":
                await confirm_platega_payment(session, payment, bot=bot)
                logger.info(f"reconcile_pending_platega: confirmed missed payment {tx_id}")
            else:
                payment.status = "chargeback" if status == "CHARGEBACKED" else "canceled"
                await session.commit()


async def check_expired_subscriptions(bot: Bot):
    logger.info("Running task: check_expired_subscriptions")
    async with async_session_maker() as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        subscriptions = (
            (
                await session.execute(
                    select(Subscription)
                    .options(joinedload(Subscription.user))
                    .where(
                        Subscription.expires_at <= now,
                        Subscription.is_active == True,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not subscriptions:
            return
        subscription_ids = [subscription.id for subscription in subscriptions]
        device_rows = (
            await session.execute(
                select(Device.subscription_id, Device.uuid).where(Device.subscription_id.in_(subscription_ids))
            )
        ).all()
        device_uuids: dict[int, list[str]] = {}
        for subscription_id, client_uuid in device_rows:
            device_uuids.setdefault(subscription_id, []).append(client_uuid)
        server_rows = (
            await session.execute(
                select(
                    SubscriptionServer.subscription_id,
                    Server.id,
                    Server.control_mode,
                    Server.agent_url,
                    Server.agent_token,
                )
                .join(Server, Server.id == SubscriptionServer.server_id)
                .where(SubscriptionServer.subscription_id.in_(subscription_ids))
            )
        ).all()
        servers_by_subscription: dict[int, list] = {}
        server_ids: set[int] = set()
        for row in server_rows:
            servers_by_subscription.setdefault(row.subscription_id, []).append(row)
            server_ids.add(row.id)

        jobs: list[_ExpiredSubscriptionJob] = []
        for subscription in subscriptions:
            subscription.is_active = False
            user = subscription.user
            jobs.append(
                _ExpiredSubscriptionJob(
                    subscription_id=subscription.id,
                    user_tg_id=user.tg_id if user is not None else None,
                    user_language=user.language if user is not None else "ru",
                    user_banned=user.is_banned if user is not None else True,
                    client_uuids=(
                        subscription.client_uuid,
                        *device_uuids.get(subscription.id, []),
                    ),
                    push_targets=tuple(
                        (row.id, row.agent_url, row.agent_token)
                        for row in servers_by_subscription.get(subscription.id, [])
                        if row.control_mode in {"push", "observe"}
                    ),
                )
            )
        # Pull snapshots are published in the same short transaction as the
        # active-state transition, before any remote or Telegram I/O begins.
        await NodeControlService.publish_for_servers(session, server_ids)
        await session.commit()

    semaphore = asyncio.Semaphore(_SCHEDULER_NETWORK_CONCURRENCY)

    async def revoke(agent_url: str, agent_token: str, client_uuid: str, server_id: int) -> None:
        async with semaphore:
            try:
                await AgentClient(agent_url, agent_token).remove_client(client_uuid)
            except Exception as exc:
                logger.error(
                    "Failed to remove client %s from server %s: %s",
                    client_uuid,
                    server_id,
                    exc,
                )

    await asyncio.gather(
        *(
            revoke(agent_url, agent_token, client_uuid, server_id)
            for job in jobs
            for server_id, agent_url, agent_token in job.push_targets
            for client_uuid in job.client_uuids
        )
    )
    for job in jobs:
        if job.user_tg_id is None or job.user_banned:
            continue
        try:
            await bot.send_message(
                job.user_tg_id,
                t(job.user_language, "expired_notice"),
                reply_markup=renewal_keyboard(job.user_language),
            )
        except Exception as exc:
            logger.error("Failed to send expiration notice to %s: %s", job.user_tg_id, exc)


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
        rows = (
            await session.execute(
                select(
                    SubscriptionServer.subscription_id,
                    SubscriptionServer.server_id,
                    Subscription.client_uuid,
                    Subscription.user_id,
                    Server.control_mode,
                    Server.agent_url,
                    Server.agent_token,
                )
                .join(Subscription, Subscription.id == SubscriptionServer.subscription_id)
                .join(Server, Server.id == SubscriptionServer.server_id)
                .where(
                    SubscriptionServer.is_synced == False,
                    Subscription.is_active == True,
                    Server.is_active == True,
                )
            )
        ).all()
    jobs = [_UnsyncedJob(*row) for row in rows]

    # Desired-state publication is local DB work. Keep it serialized because
    # SQLite has one writer, but do not hold this session across node HTTP.
    for server_id in sorted({job.server_id for job in jobs if job.control_mode == "pull"}):
        async with async_session_maker() as session:
            await NodeControlService.publish_for_servers(session, {server_id})
            await session.commit()

    semaphore = asyncio.Semaphore(_SCHEDULER_NETWORK_CONCURRENCY)

    async def retry_push(job: _UnsyncedJob) -> _UnsyncedJob | None:
        if job.control_mode == "pull":
            return None
        async with semaphore:
            client = AgentClient(job.agent_url, job.agent_token)
            try:
                email = f"user_{job.user_id}_sub_{job.subscription_id}"
                success = await client.add_client(job.client_uuid, email)
            except Exception as exc:
                logger.error(
                    "Retry sync failed for %s to server %s: %s",
                    job.client_uuid,
                    job.server_id,
                    exc,
                )
                return None
        if not success:
            return None
        logger.info("Successfully synced %s to server %s", job.client_uuid, job.server_id)
        return job

    successful = [job for job in await asyncio.gather(*(retry_push(job) for job in jobs)) if job is not None]
    if not successful:
        return
    async with async_session_maker() as session:
        for job in successful:
            await session.execute(
                update(SubscriptionServer)
                .where(
                    SubscriptionServer.subscription_id == job.subscription_id,
                    SubscriptionServer.server_id == job.server_id,
                    SubscriptionServer.is_synced == False,
                )
                .values(is_synced=True)
            )
        await session.commit()


async def _traffic_sources() -> list[_TrafficSource]:
    async with async_session_maker() as session:
        server_rows = (
            await session.execute(
                select(
                    Server.id,
                    Server.control_mode,
                    Server.agent_url,
                    Server.agent_token,
                ).where(Server.is_active == True)
            )
        ).all()
        pull_ids = [row.id for row in server_rows if row.control_mode == "pull"]
        telemetry_by_server: dict[int, dict[str, dict]] = {}
        if pull_ids:
            telemetry_rows = (
                await session.execute(
                    select(NodeTelemetry.server_id, NodeTelemetry.payload).where(NodeTelemetry.server_id.in_(pull_ids))
                )
            ).all()
            telemetry_by_server = {
                server_id: dict((payload or {}).get("stats") or {}) for server_id, payload in telemetry_rows
            }
    return [
        _TrafficSource(
            server_id=row.id,
            control_mode=row.control_mode,
            agent_url=row.agent_url,
            agent_token=row.agent_token,
            telemetry_stats=telemetry_by_server.get(row.id, {}),
        )
        for row in server_rows
    ]


async def _fetch_traffic_stats(
    source: _TrafficSource,
    semaphore: asyncio.Semaphore,
) -> tuple[int, dict]:
    if source.control_mode == "pull":
        return source.server_id, source.telemetry_stats
    async with semaphore:
        client = AgentClient(source.agent_url, source.agent_token)
        try:
            return source.server_id, await client.get_stats()
        except Exception as exc:
            logger.warning("traffic poll: server %s stats failed: %s", source.server_id, exc)
            return source.server_id, {}


async def _apply_traffic_stats(server_id: int, stats: dict) -> None:
    if not stats:
        return
    stats_index = _index_stats_by_subscription(stats)
    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        if server is None or not server.is_active:
            return
        links = (
            (
                await session.execute(
                    select(SubscriptionServer)
                    .options(joinedload(SubscriptionServer.subscription))
                    .where(SubscriptionServer.server_id == server_id)
                )
            )
            .scalars()
            .all()
        )
        changed = False
        for link in links:
            sub = link.subscription
            if sub is None:
                continue
            entries = stats_index.get((sub.user_id, sub.id), [])
            if not entries:
                continue

            cursors = dict(link.traffic_cursors or {})
            delta_up = delta_down = 0
            for email, cur in entries:
                cur_up = int(cur.get("uplink", 0) or 0)
                cur_down = int(cur.get("downlink", 0) or 0)
                last = cursors.get(email)
                if last is None:
                    cursors[email] = [cur_up, cur_down]
                    continue
                d_up = cur_up - int(last[0])
                if d_up < 0:
                    d_up = cur_up
                d_down = cur_down - int(last[1])
                if d_down < 0:
                    d_down = cur_down
                delta_up += d_up
                delta_down += d_down
                cursors[email] = [cur_up, cur_down]

            if delta_up:
                sub.traffic_up_bytes = (sub.traffic_up_bytes or 0) + delta_up
                link.traffic_up_bytes = (link.traffic_up_bytes or 0) + delta_up
                server.traffic_up_bytes = (server.traffic_up_bytes or 0) + delta_up
                changed = True
            if delta_down:
                sub.traffic_down_bytes = (sub.traffic_down_bytes or 0) + delta_down
                link.traffic_down_bytes = (link.traffic_down_bytes or 0) + delta_down
                server.traffic_down_bytes = (server.traffic_down_bytes or 0) + delta_down
                changed = True
            if cursors != (link.traffic_cursors or {}):
                link.traffic_cursors = cursors
                changed = True

        if changed:
            await session.commit()


async def poll_traffic():
    """Fetch counters outside DB sessions, then apply one short batch per node."""
    logger.info("Running task: poll_traffic")
    sources = await _traffic_sources()
    semaphore = asyncio.Semaphore(_SCHEDULER_NETWORK_CONCURRENCY)
    results = await asyncio.gather(*(_fetch_traffic_stats(source, semaphore) for source in sources))
    for server_id, stats in results:
        await _apply_traffic_stats(server_id, stats)


def _make_full_backup() -> Path | None:
    """Create an encrypted disaster-recovery archive containing:
      - aegis.db  (consistent SQLite snapshot via online backup API)
      - agent.env (Reality keys + agent token)
      - bot.env   (bot token, admin IDs and other critical settings)

    The plaintext tar exists only in memory. The only persistent output is an
    authenticated ciphertext decryptable by the offline recovery private key.
    Returns the encrypted archive path, or None if the DB is not SQLite.
    """
    if not settings.db_url.startswith("sqlite+aiosqlite"):
        return None

    src = settings.sqlite_path
    if not Path(src).exists():
        logger.warning("backup: db file %s missing", src)
        return None

    public_key_path = Path(settings.backup_public_key_file)
    try:
        public_key_pem = public_key_path.read_bytes()
    except OSError as exc:
        raise BackupConfigurationError(f"backup public key is unavailable at {public_key_path}") from exc

    out_dir = Path(settings.backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_dir.chmod(0o700)

    stamp = datetime.now(UTC).strftime("%d.%m.%Y-%H:%M")
    archive = out_dir / f"AegisVPN-BACKUP-{stamp}.aegis"

    with tempfile.TemporaryDirectory(prefix=".backup-", dir=out_dir) as temp_dir:
        Path(temp_dir).chmod(0o700)
        db_snap = Path(temp_dir) / "aegis.db"
        dst = sqlite3.connect(db_snap)
        try:
            with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as live:
                live.backup(dst)
        finally:
            dst.close()
        db_snap.chmod(0o600)

        lines = [
            f"BOT_TOKEN={settings.bot_token.get_secret_value()}",
            f"ADMIN_IDS={json.dumps(settings.admin_ids)}",
        ]
        for key, val in [
            ("BOT_DOMAIN", settings.bot_domain),
            ("PUBLIC_BASE_URL", settings.public_base_url),
            ("SUBSCRIPTION_PUBLIC_BASE_URL", settings.subscription_public_base_url),
            ("BOT_PUBLIC_URL", settings.bot_public_url),
            ("SUPPORT_PUBLIC_URL", settings.support_public_url),
            ("SITE_PUBLIC_URL", settings.site_public_url),
            ("TELEGRAM_MODE", settings.telegram_mode),
            ("WEBAPP_PORT", settings.webapp_port),
            ("SITE_TITLE", settings.site_title),
            ("SUBSCRIPTION_TITLE", settings.subscription_title),
        ]:
            if val is not None:
                lines.append(f"{key}={val}")
        bot_env = ("\n".join(lines) + "\n").encode()

        plaintext = io.BytesIO()
        with tarfile.open(fileobj=plaintext, mode="w:gz") as tar:
            _add_backup_bytes(tar, "aegis.db", db_snap.read_bytes())
            _add_backup_bytes(tar, "bot.env", bot_env)
            agent_env = Path(settings.bootstrap_server_agent_env)
            if agent_env.exists():
                _add_backup_bytes(tar, "agent.env", agent_env.read_bytes())
            else:
                logger.warning("backup: agent.env not found at %s", agent_env)

        encrypted = encrypt_backup(plaintext.getvalue(), public_key_pem)

    fd, temp_name = tempfile.mkstemp(prefix=".encrypted-", dir=out_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, archive)
        archive.chmod(0o600)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)

    backups = sorted(out_dir.glob("AegisVPN-BACKUP-*.aegis"))
    for old in backups[: max(0, len(backups) - max(1, settings.backup_keep))]:
        old.unlink(missing_ok=True)

    return archive


def _add_backup_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = int(datetime.now(UTC).timestamp())
    tar.addfile(info, io.BytesIO(data))


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
    caption = f"Зашифрованный бэкап {datetime.now(UTC):%Y-%m-%d %H:%M} UTC ({len(data) // 1024} KiB)"

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


async def _check_one(
    server: Server,
    *,
    telemetry: NodeTelemetry | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, int | None]:
    """Return (ok, reason, clients) for a single server."""
    clients: int | None = None
    reasons: list[str] = []
    if server.control_mode == "pull":
        current_time = now or datetime.now(UTC).replace(tzinfo=None)
        observed_times = [
            value
            for value in (
                server.control_last_seen_at,
                telemetry.received_at if telemetry is not None else None,
            )
            if value is not None
        ]
        freshest = max(observed_times) if observed_times else None
        max_age = timedelta(seconds=max(1.0, settings.node_control_health_max_age_seconds))
        if freshest is None or current_time - freshest > max_age:
            reasons.append("control heartbeat/telemetry stale")
        if int(server.desired_generation or 0) != int(server.applied_generation or 0):
            reasons.append(
                f"generation desired={server.desired_generation or 0} applied={server.applied_generation or 0}"
            )
        if server.control_last_error:
            reasons.append(f"control error={server.control_last_error}")
        if telemetry is not None:
            online = (telemetry.payload or {}).get("online_emails")
            if isinstance(online, list):
                clients = len(online)
        agent_ok = not reasons
    else:
        client = AgentClient(server.agent_url, server.agent_token)
        agent_ok = False
        try:
            health = await client.get_health()
            agent_ok = health.get("status") == "ok"
            clients = health.get("clients")
            if not agent_ok:
                reasons.append(f"agent status={health.get('status')!r}")
        except Exception as exc:
            reasons.append(f"agent unreachable ({type(exc).__name__})")

    xray_ok = await _probe_xray_port(server.host, server.port)
    if not xray_ok:
        reasons.append(f"xray port {server.port} closed")

    return agent_ok and xray_ok, "; ".join(reasons), clients


async def check_servers_health(bot: Bot):
    logger.info("Running task: check_servers_health")
    async with async_session_maker() as session:
        result = await session.execute(select(Server).where(Server.is_active == True))
        servers = result.scalars().all()
        telemetry_by_server = {
            row.server_id: row
            for row in (
                await session.execute(
                    select(NodeTelemetry).where(NodeTelemetry.server_id.in_([server.id for server in servers]))
                )
            )
            .scalars()
            .all()
        }

    semaphore = asyncio.Semaphore(8)

    async def check_with_retry(server: Server) -> tuple[Server, bool, str, int | None]:
        async with semaphore:
            telemetry = telemetry_by_server.get(server.id)
            ok, reason, clients = await _check_one(server, telemetry=telemetry)
            if not ok:
                await asyncio.sleep(3)
                ok, reason, clients = await _check_one(server, telemetry=telemetry)
            return server, ok, reason, clients

    results = await asyncio.gather(*(check_with_retry(server) for server in servers))
    for server, ok, reason, clients in results:
        was_down = server.id in _down_servers
        if ok:
            _fail_counts.pop(server.id, None)
            if was_down:
                _down_servers.discard(server.id)
                logger.info("Server %s (%s) recovered", server.id, server.name)
                await _alert_admins(
                    bot,
                    f"Нода снова в строю: {server.name}\n{server.host}:{server.port}",
                )
            continue

        fails = _fail_counts.get(server.id, 0) + 1
        _fail_counts[server.id] = fails
        logger.warning(
            "health: %s (%s) failing %d/%d — %s",
            server.name,
            server.host,
            fails,
            HEALTH_FAIL_THRESHOLD,
            reason,
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
