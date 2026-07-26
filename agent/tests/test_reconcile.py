import json

import pytest

from app import connlimit, reconcile, xray
from app.control_models import DesiredSnapshot


def _config() -> dict:
    return {
        "inbounds": [
            {
                "tag": "vless-xhttp",
                "port": 443,
                "protocol": "vless",
                "settings": {
                    "clients": [
                        {"id": "keep", "email": "user_1_sub_1"},
                        {"id": "stale-base", "email": "user_2_sub_2"},
                        {"id": "stale-device", "email": "user_2_sub_2_dev_3"},
                    ]
                },
                "streamSettings": {"network": "xhttp"},
            },
            {
                "tag": "vless-tcp",
                "port": 2053,
                "protocol": "vless",
                "settings": {
                    "clients": [
                        {
                            "id": "keep",
                            "email": "user_1_sub_1",
                            "flow": "xtls-rprx-vision",
                        },
                        {
                            "id": "stale-base",
                            "email": "user_2_sub_2",
                            "flow": "xtls-rprx-vision",
                        },
                        {
                            "id": "stale-device",
                            "email": "user_2_sub_2_dev_3",
                            "flow": "xtls-rprx-vision",
                        },
                    ]
                },
                "streamSettings": {"network": "tcp"},
            },
            {"tag": "api", "protocol": "dokodemo-door", "settings": {}},
        ]
    }


def _snapshot() -> DesiredSnapshot:
    return DesiredSnapshot(
        generation=8,
        digest="8" * 64,
        items=[
            {
                "kind": "client",
                "uuid": "keep",
                "email": "user_1_sub_1",
                "expire_ms": 4_102_444_800_000,
            },
            {
                "kind": "client",
                "uuid": "new-device",
                "email": "user_1_sub_1_dev_4",
                "expire_ms": 4_102_444_800_000,
            },
            {"kind": "conn_limit", "user_id": 1, "limit": 2},
        ],
    )


async def _patch_runtime(monkeypatch, tmp_path):
    config_path = tmp_path / "xray.json"
    config_path.write_text(json.dumps(_config()))
    monkeypatch.setattr(xray.settings, "xray_config_path", str(config_path))
    monkeypatch.setattr(
        reconcile,
        "_APPLIED_STATE_PATH",
        str(tmp_path / "applied-state.json"),
    )
    monkeypatch.setattr(connlimit, "_OVERRIDES_PATH", str(tmp_path / "conn-limits.json"))
    connlimit._overrides.clear()

    additions: list[tuple[str, dict]] = []
    removals: list[tuple[str, str]] = []
    events: list[tuple[str, object]] = []

    async def add(inbound, record):
        additions.append((inbound["tag"], record))
        return True

    async def remove(tag, email):
        removals.append((tag, email))
        return True

    def refresh(config):
        events.append(("refresh", config))

    async def kick(emails):
        events.append(("kick", list(emails)))
        return True

    monkeypatch.setattr(reconcile, "xray_api_add", add)
    monkeypatch.setattr(reconcile, "xray_api_remove", remove)
    monkeypatch.setattr(reconcile.hysteria, "refresh_from_config", refresh)
    monkeypatch.setattr(reconcile.hysteria, "kick", kick)
    monkeypatch.setattr(reconcile, "reload_xray", lambda: None)
    return config_path, additions, removals, events


async def test_reconcile_applies_exact_state_and_is_idempotent(monkeypatch, tmp_path):
    config_path, additions, removals, events = await _patch_runtime(
        monkeypatch,
        tmp_path,
    )

    result = await reconcile.reconcile_snapshot(_snapshot(), observe=False)

    assert result.success is True
    assert result.changed is True
    assert result.added == 2
    assert result.removed == 4
    config = json.loads(config_path.read_text())
    vless = [item for item in config["inbounds"] if item["protocol"] == "vless"]
    assert {client["id"] for client in vless[0]["settings"]["clients"]} == {
        "keep",
        "new-device",
    }
    assert all(
        "flow" not in client for client in vless[0]["settings"]["clients"]
    )
    assert all(
        client["flow"] == "xtls-rprx-vision"
        for client in vless[1]["settings"]["clients"]
    )
    assert {
        (tag, record["id"], record.get("flow"))
        for tag, record in additions
    } == {
        ("vless-xhttp", "new-device", None),
        ("vless-tcp", "new-device", "xtls-rprx-vision"),
    }
    assert len(removals) == 4
    assert events[-2][0] == "refresh"
    assert events[-1] == (
        "kick",
        ["user_2_sub_2", "user_2_sub_2_dev_3"],
    )
    assert connlimit._overrides == {1: 2}
    applied = reconcile.load_applied_state()
    assert applied.generation == 8
    assert applied.digest == "8" * 64
    assert len(applied.items) == 3

    additions.clear()
    removals.clear()
    events.clear()
    repeated = await reconcile.reconcile_snapshot(_snapshot(), observe=False)
    assert repeated.success is True
    assert repeated.changed is False
    assert additions == removals == events == []


async def test_observe_reports_diff_without_mutating(monkeypatch, tmp_path):
    config_path, additions, removals, events = await _patch_runtime(
        monkeypatch,
        tmp_path,
    )
    before = config_path.read_bytes()

    result = await reconcile.reconcile_snapshot(_snapshot(), observe=True)

    assert result.success is True
    assert result.observed is True
    assert result.changed is True
    assert config_path.read_bytes() == before
    assert additions == removals == events == []
    assert reconcile.load_applied_state().generation == 0


async def test_failed_live_apply_never_advances_applied_state(monkeypatch, tmp_path):
    _, _, _, _ = await _patch_runtime(monkeypatch, tmp_path)

    async def fail_add(inbound, record):
        return False

    monkeypatch.setattr(reconcile, "xray_api_add", fail_add)
    monkeypatch.setattr(reconcile, "wait_for_xray_ready", _false)

    with pytest.raises(reconcile.ReconcileError, match="live Xray"):
        await reconcile.reconcile_snapshot(_snapshot(), observe=False)

    assert reconcile.load_applied_state().generation == 0


async def _false():
    return False


async def _true():
    return True


async def test_verified_reload_fallback_can_advance_applied_state(
    monkeypatch,
    tmp_path,
):
    _, _, _, _ = await _patch_runtime(monkeypatch, tmp_path)

    async def fail_add(inbound, record):
        return False

    reloads: list[bool] = []
    monkeypatch.setattr(reconcile, "xray_api_add", fail_add)
    monkeypatch.setattr(
        reconcile,
        "reload_xray",
        lambda: reloads.append(True),
    )
    monkeypatch.setattr(reconcile, "wait_for_xray_ready", _true)

    result = await reconcile.reconcile_snapshot(_snapshot(), observe=False)

    assert result.success is True
    assert reloads == [True]
    assert reconcile.load_applied_state().generation == 8


async def test_cached_snapshot_expires_clients_without_new_generation(
    monkeypatch,
    tmp_path,
):
    config_path, _, _, _ = await _patch_runtime(monkeypatch, tmp_path)
    snapshot = DesiredSnapshot(
        generation=9,
        digest="9" * 64,
        items=[
            {
                "kind": "client",
                "uuid": "keep",
                "email": "user_1_sub_1",
                "expire_ms": 2_000,
            }
        ],
    )
    monkeypatch.setattr(reconcile, "wait_for_xray_ready", _true)

    await reconcile.reconcile_snapshot(
        snapshot,
        observe=False,
        now_ms=1_000,
    )
    await reconcile.reconcile_snapshot(
        snapshot,
        observe=False,
        now_ms=3_000,
    )

    config = json.loads(config_path.read_text())
    clients = config["inbounds"][0]["settings"]["clients"]
    assert clients == []


def test_replace_overrides_replaces_and_persists_complete_map(monkeypatch, tmp_path):
    path = tmp_path / "conn-limits.json"
    monkeypatch.setattr(connlimit, "_OVERRIDES_PATH", str(path))
    connlimit._overrides = {99: 9}

    connlimit.replace_overrides({1: 2, 2: 0})

    assert connlimit._overrides == {1: 2, 2: 0}
    assert json.loads(path.read_text()) == {"1": 2, "2": 0}


def test_corrupt_applied_state_recovers_as_unapplied(monkeypatch, tmp_path):
    path = tmp_path / "applied-state.json"
    path.write_text("{truncated")
    monkeypatch.setattr(reconcile, "_APPLIED_STATE_PATH", str(path))

    state = reconcile.load_applied_state()

    assert state.generation == 0
    assert state.digest is None
    assert state.items == []


def test_interrupted_atomic_state_write_preserves_previous_file(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "applied-state.json"
    previous = b'{"generation":1,"digest":null,"items":[]}'
    path.write_bytes(previous)

    def interrupted(_source, _target):
        raise OSError("simulated interruption")

    monkeypatch.setattr(reconcile.os, "replace", interrupted)

    with pytest.raises(OSError, match="simulated"):
        reconcile._atomic_write_json(
            str(path),
            {"generation": 2, "digest": "2" * 64, "items": []},
        )

    assert path.read_bytes() == previous
    assert list(tmp_path.glob(".control-state-*.tmp")) == []
