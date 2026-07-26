import hashlib
import json

import pytest

from app.control_client import (
    ControlClient,
    ControlProtocolError,
    RawControlResponse,
    build_ssl_context,
)
from app.control_models import AppliedState


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _response(payload: object, status: int = 200) -> RawControlResponse:
    return RawControlResponse(
        status=status,
        body=_canonical(payload),
    )


async def test_client_downloads_and_verifies_paginated_snapshot():
    items = [
        {
            "kind": "client",
            "uuid": "10000000-0000-0000-0000-000000000001",
            "email": "user_1_sub_2",
            "expire_ms": 1234,
        },
        {"kind": "conn_limit", "user_id": 1, "limit": 3},
    ]
    pages = [items[:1], items[1:]]
    manifest = {
        "schema_version": 1,
        "generation": 7,
        "digest": hashlib.sha256(_canonical(items)).hexdigest(),
        "item_count": 2,
        "page_count": 2,
        "page_size": 1,
    }
    calls: list[tuple[str, str, dict]] = []
    ssl_context = object()

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/sync"):
            return _response(manifest)
        page_index = int(url.rsplit("/", 1)[-1])
        page_items = pages[page_index]
        return _response(
            {
                "schema_version": 1,
                "generation": 7,
                "page_index": page_index,
                "page_digest": hashlib.sha256(_canonical(page_items)).hexdigest(),
                "items": page_items,
            }
        )

    client = ControlClient(
        urls=["https://control.example"],
        token="node-secret",
        ssl_context=ssl_context,
        requester=request,
    )
    snapshot = await client.sync(AppliedState(generation=0, digest=None))

    assert snapshot is not None
    assert snapshot.generation == 7
    assert [item.kind for item in snapshot.items] == ["client", "conn_limit"]
    assert [call[1] for call in calls] == [
        "https://control.example/api/node/v1/sync",
        "https://control.example/api/node/v1/snapshots/7/pages/0",
        "https://control.example/api/node/v1/snapshots/7/pages/1",
    ]
    assert all(call[2]["headers"]["Authorization"] == "Bearer node-secret" for call in calls)
    assert all(call[2]["ssl"] is ssl_context for call in calls)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("page-digest", "page digest"),
        ("manifest-digest", "snapshot digest"),
        ("duplicate-uuid", "duplicate client UUID"),
        ("unsupported-schema", "schema version"),
    ],
)
async def test_client_rejects_untrusted_snapshot_content(mutation, message):
    client_item = {
        "kind": "client",
        "uuid": "20000000-0000-0000-0000-000000000001",
        "email": "user_1_sub_2",
        "expire_ms": 1234,
    }
    items = [client_item]
    if mutation == "duplicate-uuid":
        items.append(dict(client_item))
    page_digest = hashlib.sha256(_canonical(items)).hexdigest()
    manifest_digest = hashlib.sha256(_canonical(items)).hexdigest()
    schema_version = 2 if mutation == "unsupported-schema" else 1
    if mutation == "page-digest":
        page_digest = "0" * 64
    if mutation == "manifest-digest":
        manifest_digest = "0" * 64

    async def request(method, url, **kwargs):
        if url.endswith("/sync"):
            return _response(
                {
                    "schema_version": schema_version,
                    "generation": 1,
                    "digest": manifest_digest,
                    "item_count": len(items),
                    "page_count": 1,
                    "page_size": len(items),
                }
            )
        return _response(
            {
                "schema_version": 1,
                "generation": 1,
                "page_index": 0,
                "page_digest": page_digest,
                "items": items,
            }
        )

    client = ControlClient(
        urls=["https://control.example"],
        token="node-secret",
        ssl_context=object(),
        requester=request,
    )
    with pytest.raises(ControlProtocolError, match=message):
        await client.sync(AppliedState(generation=0, digest=None))


async def test_client_fails_over_and_bounds_response_bytes():
    calls: list[str] = []

    async def failover_request(method, url, **kwargs):
        calls.append(url)
        if url.startswith("https://first.example"):
            raise OSError("first endpoint unavailable")
        return RawControlResponse(status=204, body=b"")

    client = ControlClient(
        urls=["https://first.example", "https://second.example"],
        token="node-secret",
        ssl_context=object(),
        requester=failover_request,
    )
    assert await client.sync(AppliedState(generation=3, digest="a" * 64)) is None
    assert calls == [
        "https://first.example/api/node/v1/sync",
        "https://second.example/api/node/v1/sync",
    ]

    async def oversized_request(method, url, **kwargs):
        return RawControlResponse(status=200, body=b"x" * 65)

    bounded = ControlClient(
        urls=["https://control.example"],
        token="node-secret",
        ssl_context=object(),
        requester=oversized_request,
        max_page_bytes=64,
    )
    with pytest.raises(ControlProtocolError, match="response exceeds"):
        await bounded.sync(AppliedState(generation=0, digest=None))


def test_ssl_context_loads_control_ca_and_node_identity(monkeypatch):
    calls: list[tuple] = []

    class FakeContext:
        def load_cert_chain(self, certfile, keyfile):
            calls.append(("identity", certfile, keyfile))

    def fake_default_context(*, cafile):
        calls.append(("ca", cafile))
        return FakeContext()

    monkeypatch.setattr("app.control_client.ssl.create_default_context", fake_default_context)

    context = build_ssl_context(
        ca_path="/control/ca.crt",
        cert_path="/control/client.crt",
        key_path="/control/client.key",
    )

    assert isinstance(context, FakeContext)
    assert calls == [
        ("ca", "/control/ca.crt"),
        ("identity", "/control/client.crt", "/control/client.key"),
    ]
