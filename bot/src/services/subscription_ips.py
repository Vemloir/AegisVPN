"""Cross-node union of recent direct VLESS sources; observation, not enforcement."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import NodeTelemetry

MAX_AGE = timedelta(seconds=90)


@dataclass(frozen=True)
class Count:
    ips: int | None
    fresh: int
    expected: int

    def describe(self) -> str:
        count = "нет сопоставимых данных" if self.ips is None else str(self.ips)
        if self.ips is not None and self.fresh < self.expected:
            count += " (неполный подсчёт)"
        return f"Уникальные IP подписки (прямой VLESS): {count}. Свежие данные: {self.fresh}/{self.expected} локаций."


def aggregate(samples: dict[int, dict], expected: set[int], sub_id: int, now: datetime) -> Count:
    seen: set[str] = set()
    keys: set[str] = set()
    fresh = 0
    timestamp = (now if now.tzinfo else now.replace(tzinfo=UTC)).timestamp()
    for node_id in expected:
        s = samples.get(node_id)
        if not isinstance(s, dict) or type(s.get("sampled_at")) is not int:
            continue
        if not timestamp - MAX_AGE.total_seconds() <= s["sampled_at"] <= timestamp + 5:
            continue
        key = s.get("key_id")
        subscriptions = s.get("subscriptions")
        if not isinstance(key, str) or not re.fullmatch("[0-9a-f]{16}", key) or not isinstance(subscriptions, dict):
            continue
        tokens = subscriptions.get(str(sub_id), [])
        if not isinstance(tokens, list) or any(
            not isinstance(t, str) or not re.fullmatch("[0-9a-f]{32}", t) for t in tokens
        ):
            continue
        keys.add(key)
        seen.update(tokens)
        fresh += 1
    return Count(len(seen) if len(keys) == 1 else None, fresh, len(expected))


async def for_subscription(session: AsyncSession, sub_id: int, node_ids: set[int], now: datetime) -> Count:
    if not node_ids:
        return Count(None, 0, 0)
    # Latest accepted sequence per node only; never add successive snapshots.
    rows = await session.execute(
        select(NodeTelemetry.server_id, NodeTelemetry.payload["subscription_ips_v1"]).where(
            NodeTelemetry.server_id.in_(node_ids),
            NodeTelemetry.received_at >= now - MAX_AGE,
        )
    )
    return aggregate(dict(rows.all()), node_ids, sub_id, now)
