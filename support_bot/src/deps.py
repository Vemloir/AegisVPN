"""Shared singletons (storage + operator set) imported by handlers and main."""

from .config import settings
from .storage import Storage

storage = Storage(settings.db_path)
ADMIN_IDS = set(settings.admin_ids)
