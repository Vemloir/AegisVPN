"""Shared helpers for the admin handlers."""

from src.core.config import settings


def is_admin(tg_id: int) -> bool:
    return tg_id in settings.admin_ids


def fmt_bytes(n: int | None) -> str:
    n = int(n or 0)
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if (v >= 100 or i == 0) else f"{v:.1f} {units[i]}"
