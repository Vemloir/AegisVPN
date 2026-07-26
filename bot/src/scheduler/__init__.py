import inspect
from collections.abc import Callable
from functools import wraps

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.config import settings
from src.core.leader import leader_lease

from .tasks import (
    backup_database,
    check_expired_subscriptions,
    check_servers_health,
    poll_traffic,
    reconcile_pending_platega,
    remind_expiring_subscriptions,
    retry_unsynced_servers,
    send_backup_to_admins,
)


def singleton_job(
    name: str,
    function: Callable,
    *,
    lease_factory=leader_lease,
) -> Callable:
    """Run one scheduler invocation across the whole PostgreSQL cluster."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        async with lease_factory(f"scheduler:{name}") as lease:
            if not lease.acquired:
                return None
            result = function(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

    return wrapped


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    scheduler.add_job(singleton_job("expire", check_expired_subscriptions), "interval", minutes=1, args=[bot])
    scheduler.add_job(singleton_job("reminders", remind_expiring_subscriptions), "cron", hour="*", args=[bot])
    scheduler.add_job(singleton_job("retry-sync", retry_unsynced_servers), "interval", minutes=5)
    scheduler.add_job(singleton_job("health", check_servers_health), "interval", minutes=1, args=[bot])
    scheduler.add_job(singleton_job("traffic", poll_traffic), "interval", minutes=2)
    scheduler.add_job(singleton_job("platega", reconcile_pending_platega), "interval", minutes=3, args=[bot])
    scheduler.add_job(singleton_job("backup", backup_database), "cron", hour=settings.backup_hour, args=[bot])
    scheduler.add_job(singleton_job("backup-send", send_backup_to_admins), "interval", hours=1, args=[bot])

    return scheduler
