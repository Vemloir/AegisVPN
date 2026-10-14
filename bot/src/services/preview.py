"""Who sees preview locations (see Settings.preview_tg_ids)."""

from __future__ import annotations

from src.core.config import settings
from src.models import Server


def sees_previews(tg_id: int | None) -> bool:
    return tg_id is not None and tg_id in set(settings.preview_tg_ids)


def visible_servers(servers: list[Server], tg_id: int | None) -> list[Server]:
    """``servers`` without the preview-only ones, unless ``tg_id`` sees previews."""
    if sees_previews(tg_id):
        return list(servers)
    return [server for server in servers if not getattr(server, "preview_only", False)]
