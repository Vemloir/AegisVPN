"""Offline unit tests for the Hysteria2 control plane and its merge into the
/stats and /online-emails endpoints. No network: all Hy2 HTTP calls are gated on
hy2_enabled (default False) or monkeypatched to fixed dicts.
"""

import pytest

from app import hysteria, main

SAMPLE_CONFIG = {
    "inbounds": [
        {
            "protocol": "vless",
            "tag": "xhttp",
            "settings": {
                "clients": [
                    {"id": "uuid-aaa", "email": "user_1_sub_1"},
                    {"id": "uuid-bbb", "email": "user_2_sub_5_dev_3"},
                ]
            },
        },
        # A non-vless inbound (e.g. api) must be ignored.
        {"protocol": "dokodemo-door", "tag": "api", "settings": {}},
        # A second vless inbound with a client missing an email is skipped.
        {
            "protocol": "vless",
            "tag": "tcp",
            "settings": {
                "clients": [
                    {"id": "uuid-aaa", "email": "user_1_sub_1"},
                    {"id": "uuid-noemail"},
                ]
            },
        },
    ]
}


@pytest.fixture(autouse=True)
def _reset_clients():
    hysteria._clients = {}
    yield
    hysteria._clients = {}


def test_refresh_from_config_builds_uuid_to_email():
    hysteria.refresh_from_config(SAMPLE_CONFIG)
    assert hysteria._clients == {
        "uuid-aaa": "user_1_sub_1",
        "uuid-bbb": "user_2_sub_5_dev_3",
    }


def test_refresh_from_config_empty_when_no_vless():
    hysteria.refresh_from_config({"inbounds": [{"protocol": "dokodemo-door"}]})
    assert hysteria._clients == {}


def test_authenticate_known_uuid_returns_ok_and_email():
    hysteria.refresh_from_config(SAMPLE_CONFIG)
    assert hysteria.authenticate("uuid-aaa") == {"ok": True, "id": "user_1_sub_1"}
    assert hysteria.authenticate("uuid-bbb") == {"ok": True, "id": "user_2_sub_5_dev_3"}


def test_authenticate_unknown_uuid_returns_not_ok():
    hysteria.refresh_from_config(SAMPLE_CONFIG)
    assert hysteria.authenticate("nope") == {"ok": False}
    assert hysteria.authenticate("") == {"ok": False}


async def test_traffic_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(hysteria.settings, "hy2_enabled", False)
    assert await hysteria.traffic() == {}


async def test_online_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(hysteria.settings, "hy2_enabled", False)
    assert await hysteria.online() == []


async def test_kick_noop_when_disabled_or_empty(monkeypatch):
    monkeypatch.setattr(hysteria.settings, "hy2_enabled", False)
    assert await hysteria.kick(["user_1_sub_1"]) is True
    monkeypatch.setattr(hysteria.settings, "hy2_enabled", True)
    # Empty list short-circuits before any network call.
    assert await hysteria.kick([]) is True


async def test_traffic_normalizes_tx_rx_to_uplink_downlink(monkeypatch):
    monkeypatch.setattr(hysteria.settings, "hy2_enabled", True)

    def fake_request(method, path, body=None):
        assert method == "GET"
        assert path == "/traffic"
        return b'{"user_1_sub_1": {"tx": 100, "rx": 200}, "bad": "x"}'

    monkeypatch.setattr(hysteria, "_request", fake_request)
    out = await hysteria.traffic()
    assert out == {"user_1_sub_1": {"uplink": 100, "downlink": 200}}


async def test_online_returns_keys(monkeypatch):
    monkeypatch.setattr(hysteria.settings, "hy2_enabled", True)
    monkeypatch.setattr(
        hysteria,
        "_request",
        lambda method, path, body=None: b'{"user_1_sub_1": 2, "user_2_sub_5_dev_3": 1}',
    )
    assert set(await hysteria.online()) == {"user_1_sub_1", "user_2_sub_5_dev_3"}


async def test_traffic_failsafe_on_bad_response(monkeypatch):
    monkeypatch.setattr(hysteria.settings, "hy2_enabled", True)
    monkeypatch.setattr(hysteria, "_request", lambda *a, **k: None)
    assert await hysteria.traffic() == {}
    monkeypatch.setattr(hysteria, "_request", lambda *a, **k: b"not json")
    assert await hysteria.traffic() == {}


async def test_stats_endpoint_merges_hy2_traffic(monkeypatch):
    async def fake_xray_stats():
        return {"user_1_sub_1": {"uplink": 10, "downlink": 20}}

    async def fake_hy2_traffic():
        return {
            "user_1_sub_1": {"uplink": 5, "downlink": 7},  # same email -> summed
            "user_9_sub_9": {"uplink": 1, "downlink": 2},  # Hy2-only -> added
        }

    monkeypatch.setattr(main, "query_traffic_stats", fake_xray_stats)
    monkeypatch.setattr(main.hysteria, "traffic", fake_hy2_traffic)

    result = await main.get_stats()
    assert result["stats"]["user_1_sub_1"] == {"uplink": 15, "downlink": 27}
    assert result["stats"]["user_9_sub_9"] == {"uplink": 1, "downlink": 2}


async def test_online_emails_endpoint_unions_and_dedups(monkeypatch):
    async def fake_xray_online():
        return ["user_1_sub_1", "user_2_sub_5_dev_3"]

    async def fake_hy2_online():
        return ["user_2_sub_5_dev_3", "user_9_sub_9"]  # one overlap, one new

    monkeypatch.setattr(main, "get_online_emails", fake_xray_online)
    monkeypatch.setattr(main.hysteria, "online", fake_hy2_online)

    result = await main.online_emails()
    assert set(result["emails"]) == {"user_1_sub_1", "user_2_sub_5_dev_3", "user_9_sub_9"}
    # No duplicates.
    assert len(result["emails"]) == 3


def test_hy2_auth_endpoint_returns_authenticate_shape():
    from app.models import Hy2AuthRequest

    hysteria.refresh_from_config(SAMPLE_CONFIG)
    import asyncio

    ok = asyncio.run(main.hy2_auth(Hy2AuthRequest(auth="uuid-aaa", addr="1.2.3.4:5", tx=99)))
    assert ok == {"ok": True, "id": "user_1_sub_1"}
    bad = asyncio.run(main.hy2_auth(Hy2AuthRequest(auth="unknown")))
    assert bad == {"ok": False}
