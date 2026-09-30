import asyncio
import hashlib

import pytest

from app import lbprobe
from app.config import settings

UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROBE_ID = hashlib.sha256(UUID.encode()).hexdigest()[:16]


def test_load_penalty_follows_queueing_delay():
    assert lbprobe.penalty_ms(0.0) == 0
    assert lbprobe.penalty_ms(0.5) == pytest.approx(settings.lb_load_base_ms)
    assert lbprobe.penalty_ms(0.8) == pytest.approx(4 * settings.lb_load_base_ms)
    # Past full load the node stops answering instead of reporting a number.
    assert lbprobe.penalty_ms(settings.lb_full_load) is None


def test_switch_penalty_spares_the_users_current_node():
    probe = lbprobe.LoadProbe()
    probe._active = {PROBE_ID}
    assert probe.delay_ms(PROBE_ID) == 0
    assert probe.delay_ms("0123456789abcdef") == settings.lb_switch_penalty_ms


async def _serve(probe, request: bytes) -> bytes:
    server = await asyncio.start_server(probe.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    server.close()
    return data


async def test_probe_answers_204_when_not_full():
    probe = lbprobe.LoadProbe()
    probe._active = {PROBE_ID}
    data = await _serve(probe, f"GET /p/{PROBE_ID} HTTP/1.1\r\nHost: lb\r\n\r\n".encode())
    assert data.startswith(b"HTTP/1.1 204")


async def test_full_node_does_not_answer():
    probe = lbprobe.LoadProbe()
    probe.rho = 0.99
    data = await _serve(probe, b"GET /p/x HTTP/1.1\r\n\r\n")
    assert data == b""


async def test_activity_needs_real_traffic_not_probe_bytes(monkeypatch):
    config = {"inbounds": [{"settings": {"clients": [{"id": UUID, "email": "user_1_sub_1"}]}}]}
    totals = iter([0, 2_000, 200_000])

    async def fake_config():
        return config

    async def fake_stats():
        return {"user_1_sub_1": {"uplink": 0, "downlink": next(totals)}}

    monkeypatch.setattr(lbprobe, "get_xray_config", fake_config)
    monkeypatch.setattr(lbprobe, "query_traffic_stats", fake_stats)
    probe = lbprobe.LoadProbe()
    await probe._refresh_activity()
    await probe._refresh_activity()
    assert PROBE_ID not in probe._active  # a few probe-sized bytes
    await probe._refresh_activity()
    assert PROBE_ID in probe._active
