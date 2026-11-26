from types import SimpleNamespace

import pytest
from multidict import CIMultiDict

from src.services import cdn_service


@pytest.mark.parametrize(
    "result,expected",
    [
        ("timeout", None),
        ("dns_error", None),
        ("marker", True),
        ("bad_response", False),
    ],
)
async def test_probe_distinguishes_incomplete_measurement_from_bad_response(monkeypatch, result, expected):
    class Response:
        status = 504 if result == "bad_response" else 400
        headers = CIMultiDict({"X-Padding": "x"} if result == "marker" else {})

        async def __aenter__(self):
            if result == "timeout":
                raise TimeoutError()
            if result == "dns_error":
                raise OSError("do not expose secret/path")
            return self

        async def __aexit__(self, *_args):
            pass

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trace_configs"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(cdn_service.aiohttp, "ClientSession", Session)
    node = SimpleNamespace(cdn_path="/secret-path")
    endpoint = SimpleNamespace(
        connect_address="cdn.example.com", host="cdn.example.com", tls_server_name="cdn.example.com", pin_cert=False
    )
    ok, detail = await cdn_service.probe_endpoint(node, endpoint)
    assert ok is expected
    if expected is None:
        assert detail.startswith("inconclusive:") and "stage=" in detail and "elapsed=" in detail
    assert "secret-path" not in detail
