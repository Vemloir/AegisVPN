"""poll_traffic must aggregate per-device emails (user_X_sub_Y_dev_Z), not just
the base user_X_sub_Y email, and account deltas across Xray restarts.

The whole suite shares one SQLite file (see conftest), so each test seeds its
own user/subscription/server with a unique key to avoid UNIQUE collisions.
"""

from datetime import UTC, datetime, timedelta

from src.core.database import async_session_maker, engine
from src.models import Server, Subscription, SubscriptionServer, User
from src.models.base import Base
from src.scheduler import tasks


async def _seed(key: int) -> tuple[int, str]:
    """Create a user, subscription, server and link. Returns (sub_id, email_prefix).

    Rebuilds the schema from scratch — test_migrations leaves the shared SQLite
    file with minimal tables, so create_all alone would not restore full columns.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=900000 + key)
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            sub_token=f"tok-poll-{key}",
            client_uuid=f"{key:08d}-1111-1111-1111-111111111111",
            plan_days=30,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30),
        )
        session.add(sub)

        server = Server(
            name="N",
            flag="N",
            host="1.2.3.4",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="http://127.0.0.1:8444",
            agent_token="tok",
            is_active=True,
        )
        session.add(server)
        await session.flush()

        session.add(SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True))
        await session.commit()
        return sub.id, f"user_{user.id}_sub_{sub.id}"


def _patch_stats(monkeypatch, stats: dict) -> None:
    async def fake_get_stats(self):
        return stats

    monkeypatch.setattr(tasks.AgentClient, "get_stats", fake_get_stats)


async def _totals(sub_id: int) -> tuple[int, int]:
    async with async_session_maker() as session:
        sub = await session.get(Subscription, sub_id)
        return sub.traffic_up_bytes, sub.traffic_down_bytes


async def test_per_device_emails_are_aggregated(monkeypatch):
    sub_id, prefix = await _seed(1)

    # First poll only baselines the cursors — no traffic counted yet.
    _patch_stats(
        monkeypatch,
        {
            f"{prefix}_dev_1": {"uplink": 100, "downlink": 1000},
            f"{prefix}_dev_2": {"uplink": 50, "downlink": 500},
        },
    )
    await tasks.poll_traffic()
    assert await _totals(sub_id) == (0, 0)

    # Second poll: positive deltas across both devices accumulate.
    _patch_stats(
        monkeypatch,
        {
            f"{prefix}_dev_1": {"uplink": 180, "downlink": 1200},  # +80 / +200
            f"{prefix}_dev_2": {"uplink": 75, "downlink": 700},  # +25 / +200
        },
    )
    await tasks.poll_traffic()
    assert await _totals(sub_id) == (105, 400)


async def test_xray_restart_counts_current_value(monkeypatch):
    sub_id, prefix = await _seed(2)

    _patch_stats(monkeypatch, {f"{prefix}_dev_1": {"uplink": 1000, "downlink": 2000}})
    await tasks.poll_traffic()  # baseline

    _patch_stats(monkeypatch, {f"{prefix}_dev_1": {"uplink": 1500, "downlink": 2500}})
    await tasks.poll_traffic()  # +500 / +500
    assert await _totals(sub_id) == (500, 500)

    # Xray restarted: counter dropped below the cursor → current value is the delta.
    _patch_stats(monkeypatch, {f"{prefix}_dev_1": {"uplink": 30, "downlink": 40}})
    await tasks.poll_traffic()
    assert await _totals(sub_id) == (530, 540)


async def test_base_email_still_counted(monkeypatch):
    sub_id, prefix = await _seed(3)

    _patch_stats(monkeypatch, {prefix: {"uplink": 10, "downlink": 20}})
    await tasks.poll_traffic()  # baseline
    _patch_stats(monkeypatch, {prefix: {"uplink": 60, "downlink": 120}})
    await tasks.poll_traffic()
    assert await _totals(sub_id) == (50, 100)
