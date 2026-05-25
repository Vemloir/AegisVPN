from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.config import settings

from .tasks import (
    backup_database,
    check_expired_subscriptions,
    check_servers_health,
    poll_traffic,
    remind_expiring_subscriptions,
    retry_unsynced_servers,
)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    scheduler.add_job(check_expired_subscriptions, "interval", minutes=1, args=[bot])
    scheduler.add_job(remind_expiring_subscriptions, "cron", hour="*", args=[bot])
    scheduler.add_job(retry_unsynced_servers, "interval", minutes=5)
    scheduler.add_job(check_servers_health, "interval", minutes=5, args=[bot])
    scheduler.add_job(poll_traffic, "interval", minutes=2)
    scheduler.add_job(backup_database, "cron", hour=settings.backup_hour, args=[bot])

    return scheduler
