import json
from unittest.mock import AsyncMock

from app import subscription_ips as metric


async def test_subscription_scope_canonical_ips_and_no_cdn(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("12" * 32)
    monkeypatch.setattr(metric, "KEY_FILE", key)
    raw = {
        "user_1_sub_2": {"1.1.1.1": 1, "::ffff:1.1.1.1": 2, "9.9.9.9": 3, "::1": 4, "192.168.1.2": 4},
        "user_1_sub_2_dev_3": {"1.1.1.1": 7, "2606:4700:4700::1111": 8},
        "user_1_sub_7": {"1.1.1.1": 9},
    }
    users = [*raw, "user_1_sub_2_dev_3_cdn", "relay"]
    monkeypatch.setattr(metric.connlimit, "online_users", AsyncMock(return_value=users))
    query = AsyncMock(side_effect=lambda name: raw[name])
    monkeypatch.setattr(metric.connlimit, "online_ips", query)
    monkeypatch.setattr(metric.connlimit, "cdn_peer_ips", lambda: {"9.9.9.9"})
    sample = await metric.sample()
    buckets = sample["subscriptions"]
    assert query.await_count == 3
    assert len(buckets["2"]) == 2 and len(buckets["7"]) == 1 and set(buckets["2"]).isdisjoint(buckets["7"])
    assert buckets == (await metric.sample())["subscriptions"]
    assert "1.1.1.1" not in json.dumps(sample) and "user_" not in json.dumps(sample)
    query.side_effect = metric.connlimit.StatsQueryError("unavailable")
    assert await metric.sample() is None
    key.unlink()
    assert await metric.sample() is None


async def test_valid_empty_is_not_an_error(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("ab" * 32)
    monkeypatch.setattr(metric, "KEY_FILE", key)
    monkeypatch.setattr(metric.connlimit, "online_users", AsyncMock(return_value=[]))
    assert (await metric.sample())["subscriptions"] == {}
