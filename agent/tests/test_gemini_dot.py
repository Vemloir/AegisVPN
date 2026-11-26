import asyncio
from unittest.mock import AsyncMock

from app import gemini_dot, gemini_policy


class Writer:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def reader(data):
    stream = asyncio.StreamReader()
    stream.feed_data(data)
    stream.feed_eof()
    return stream


async def test_dot_framing_tls_verification_and_no_plaintext_fallback(monkeypatch):
    query = b"\x12\x34\x01\x00" + b"\0" * 8
    answer = b"\x12\x34\x81\x80" + b"\0" * 8
    frame = len(query).to_bytes(2, "big")
    upstream = Writer()
    downstream = Writer()
    connect = AsyncMock(return_value=(reader(frame + answer), upstream))
    monkeypatch.setattr(gemini_dot.asyncio, "open_connection", connect)
    monkeypatch.setattr(gemini_policy, "load_policy", lambda: {"mode": "dns", "resolver": "tls://dns.example.com:853"})
    await gemini_dot._serve(reader(frame + query), downstream)
    assert upstream.data == frame + query and downstream.data == frame + answer
    kwargs = connect.call_args.kwargs
    assert kwargs["server_hostname"] == "dns.example.com"
    assert kwargs["ssl"].check_hostname and kwargs["ssl"].verify_mode == 2
    assert upstream.closed and downstream.closed
    assert not gemini_dot._tasks


async def test_dot_failure_never_falls_back(monkeypatch):
    connect = AsyncMock(side_effect=OSError("TLS verification failed"))
    monkeypatch.setattr(gemini_dot.asyncio, "open_connection", connect)
    monkeypatch.setattr(gemini_policy, "load_policy", lambda: {"mode": "dns", "resolver": "tls://dns.example.com"})
    downstream = Writer()
    await gemini_dot._serve(reader(b""), downstream)
    assert downstream.closed and not downstream.data
    assert connect.await_count == 1


async def test_dot_disabled_without_explicit_policy(monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(gemini_dot.asyncio, "open_connection", connect)
    monkeypatch.setattr(gemini_policy, "load_policy", lambda: None)
    downstream = Writer()
    await gemini_dot._serve(reader(b""), downstream)
    connect.assert_not_called()
    assert downstream.closed
