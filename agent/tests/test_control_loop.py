import asyncio

from app import control_loop as loop_module
from app.control_models import AppliedState, DesiredSnapshot
from app.reconcile import ReconcileResult


def _snapshot() -> DesiredSnapshot:
    return DesiredSnapshot(
        generation=3,
        digest="3" * 64,
        items=[
            {
                "kind": "client",
                "uuid": "30000000-0000-0000-0000-000000000001",
                "email": "user_1_sub_1",
                "expire_ms": 4_102_444_800_000,
            }
        ],
    )


class FakeClient:
    def __init__(self, *, snapshot=None, stop_event=None, failures=0):
        self.snapshot = snapshot
        self.stop_event = stop_event
        self.failures = failures
        self.sync_calls = 0
        self.acks: list[dict] = []
        self.telemetry: list[dict] = []
        self.closed = False

    async def sync(self, applied):
        self.sync_calls += 1
        if self.sync_calls <= self.failures:
            raise OSError("contains-sensitive-detail")
        if self.stop_event is not None:
            self.stop_event.set()
        return self.snapshot

    async def ack(self, **payload):
        self.acks.append(payload)

    async def send_telemetry(self, **payload):
        self.telemetry.append(payload)

    async def close(self):
        self.closed = True


async def test_observe_compares_without_acknowledging(monkeypatch, tmp_path):
    stop = asyncio.Event()
    client = FakeClient(snapshot=_snapshot(), stop_event=stop)
    reconciliations: list[tuple[int, bool]] = []

    async def fake_reconcile(snapshot, *, observe):
        reconciliations.append((snapshot.generation, observe))
        return ReconcileResult(
            success=True,
            changed=True,
            observed=observe,
            added=1,
            removed=0,
        )

    monkeypatch.setattr(loop_module, "reconcile_snapshot", fake_reconcile)
    monkeypatch.setattr(
        loop_module,
        "_TELEMETRY_SEQUENCE_PATH",
        str(tmp_path / "sequence"),
    )

    await loop_module.control_loop(
        stop,
        client=client,
        mode="observe",
        telemetry_builder=lambda result: _async_value({"result": result.changed}),
    )

    assert reconciliations == [(3, True)]
    assert client.acks == []
    assert client.telemetry[0]["sequence"] == 1
    assert client.closed is True


async def test_apply_acknowledges_only_successful_reconciliation(monkeypatch, tmp_path):
    stop = asyncio.Event()
    client = FakeClient(snapshot=_snapshot(), stop_event=stop)

    async def fake_reconcile(snapshot, *, observe):
        return ReconcileResult(
            success=True,
            changed=True,
            observed=observe,
            added=1,
            removed=0,
        )

    monkeypatch.setattr(loop_module, "reconcile_snapshot", fake_reconcile)
    monkeypatch.setattr(
        loop_module,
        "_TELEMETRY_SEQUENCE_PATH",
        str(tmp_path / "sequence"),
    )

    await loop_module.control_loop(
        stop,
        client=client,
        mode="apply",
        telemetry_builder=lambda result: _async_value({}),
    )

    assert client.acks == [
        {
            "generation": 3,
            "digest": "3" * 64,
            "success": True,
            "error": None,
        }
    ]


async def test_errors_back_off_with_jitter_and_do_not_log_details(
    monkeypatch,
    capsys,
    tmp_path,
):
    stop = asyncio.Event()
    client = FakeClient(snapshot=None, stop_event=stop, failures=2)
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(
        loop_module,
        "_TELEMETRY_SEQUENCE_PATH",
        str(tmp_path / "sequence"),
    )

    await loop_module.control_loop(
        stop,
        client=client,
        mode="apply",
        sleep=fake_sleep,
        jitter=lambda maximum: maximum / 2,
        telemetry_builder=lambda result: _async_value({}),
    )

    assert sleeps == [0.5, 1.0]
    output = capsys.readouterr().out
    assert "OSError" in output
    assert "contains-sensitive-detail" not in output
    assert client.closed is True


async def test_cached_snapshot_is_reconciled_when_control_is_unavailable(
    monkeypatch,
    tmp_path,
):
    stop = asyncio.Event()

    class OfflineClient(FakeClient):
        async def sync(self, applied):
            stop.set()
            raise OSError("offline")

    client = OfflineClient()
    cached = AppliedState(
        generation=5,
        digest="5" * 64,
        items=_snapshot().items,
    )
    reconciled: list[int] = []

    monkeypatch.setattr(loop_module, "load_applied_state", lambda: cached)

    async def fake_reconcile(snapshot, *, observe):
        reconciled.append(snapshot.generation)
        return ReconcileResult(
            success=True,
            changed=True,
            observed=False,
            added=0,
            removed=1,
        )

    monkeypatch.setattr(loop_module, "reconcile_snapshot", fake_reconcile)
    monkeypatch.setattr(
        loop_module,
        "_TELEMETRY_SEQUENCE_PATH",
        str(tmp_path / "sequence"),
    )

    await loop_module.control_loop(
        stop,
        client=client,
        mode="apply",
        sleep=lambda delay: _async_value(None),
        jitter=lambda maximum: 0,
        telemetry_builder=lambda result: _async_value({}),
    )

    assert reconciled == [5]


async def _async_value(value):
    return value


async def test_off_mode_starts_no_control_task(monkeypatch):
    monkeypatch.setattr(loop_module.settings, "control_mode", "off")
    assert loop_module.start_control_task() is None


async def test_telemetry_carries_safe_and_fast_subscription_templates(monkeypatch):
    async def empty_list():
        return []

    async def empty_dict():
        return {}

    async def config():
        return {
            "inbounds": [
                {
                    "tag": "safe",
                    "port": 443,
                    "protocol": "vless",
                    "settings": {"clients": []},
                    "streamSettings": {
                        "network": "xhttp",
                        "xhttpSettings": {"path": "/", "mode": "auto"},
                        "realitySettings": {"serverNames": ["safe.example"]},
                    },
                },
                {
                    "tag": "fast",
                    "port": 2053,
                    "protocol": "vless",
                    "settings": {"clients": []},
                    "streamSettings": {
                        "network": "tcp",
                        "realitySettings": {"serverNames": ["fast.example"]},
                    },
                },
            ]
        }

    monkeypatch.setattr(loop_module, "get_online_emails", empty_list)
    monkeypatch.setattr(loop_module.hysteria, "online", empty_list)
    monkeypatch.setattr(loop_module, "query_traffic_stats", empty_dict)
    monkeypatch.setattr(loop_module.hysteria, "traffic", empty_dict)
    monkeypatch.setattr(loop_module, "get_xray_config", config)
    monkeypatch.setattr(
        loop_module,
        "load_applied_state",
        lambda: AppliedState(generation=1, digest="1" * 64),
    )

    payload = await loop_module._build_telemetry(None)

    templates = {
        template["profile"]: template
        for template in payload["subscription_templates"]
    }
    assert templates["safe"]["port"] == 443
    assert ["type", "xhttp"] in templates["safe"]["query"]
    assert templates["fast"]["port"] == 2053
    assert ["flow", "xtls-rprx-vision"] in templates["fast"]["query"]
