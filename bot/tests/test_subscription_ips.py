from datetime import UTC, datetime, timedelta

from src.core.database import async_session_maker
from src.models import NodeTelemetry
from src.services.subscription_ips import Count, aggregate, for_subscription
from tests.test_gemini_admin import seed

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def sample(*tokens, ago=0, key="a" * 16):
    return {
        "sampled_at": int(NOW.timestamp()) - ago,
        "key_id": key,
        "subscriptions": {"1": [token * 32 for token in tokens], "2": ["f" * 32]},
    }


def test_union_replaces_samples_and_is_scoped_to_subscription():
    nodes = {1: sample("a", "b"), 2: sample("a"), 3: sample("c")}
    assert aggregate(nodes, {1, 2, 3}, 1, NOW) == Count(3, 3, 3)
    nodes[1] = sample()
    nodes[2] = sample("a", ago=91)
    assert aggregate(nodes, {1, 2, 3}, 1, NOW) == Count(1, 2, 3)
    assert aggregate(nodes, {1, 2, 3}, 2, NOW) == Count(1, 2, 3)
    assert aggregate(nodes, {1, 2, 3}, 3, NOW) == Count(0, 2, 3)


def test_unknown_is_not_zero_or_sum_of_different_keys():
    assert aggregate({1: sample(ago=91), 2: None}, {1, 2, 3}, 1, NOW) == Count(None, 0, 3)
    assert aggregate({1: sample("b"), 2: sample("c", key="b" * 16)}, {1, 2}, 1, NOW) == Count(None, 2, 2)
    assert aggregate({1: sample("g")}, {1}, 1, NOW) == Count(None, 0, 1)


async def test_query_uses_latest_recent_reports_only():
    a = await seed()
    now = NOW.replace(tzinfo=None)
    async with async_session_maker() as session:
        row = NodeTelemetry(server_id=a, sequence=2, received_at=now, payload={"subscription_ips_v1": sample("a")})
        session.add(row)
        await session.flush()
        assert await for_subscription(session, 1, {a, a+1}, now) == Count(1, 1, 2)
        row.received_at = now - timedelta(seconds=94)
        await session.flush()
        assert await for_subscription(session, 1, {a}, now) == Count(None, 0, 1)
