import copy
from unittest.mock import AsyncMock

import pytest

from app import gemini_admin_proxy as proxy
from app import gemini_policy, reconcile
from app.control_models import DesiredGeminiAdminProxy
from tests.test_gemini_resolver import _config, _with_warp
from tests.test_reconcile import _patch_runtime, _snapshot


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        proxy, "credentials", lambda: {"host": "dc.decodo.com", "port": 10001, "username": "test", "password": "test"}
    )


def test_scope_tls_and_priority(secret):
    cfg = _with_warp(_config())
    gemini_policy.apply_policy(cfg, {"mode": "warp", "ip_version": "ipv6"})
    cfg["routing"]["rules"].insert(1, {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"})
    before = copy.deepcopy(cfg)
    assert proxy.apply(cfg, ["user_1_sub_2", "user_1_sub_2_dev_3"])
    assert not proxy.apply(cfg, ["user_1_sub_2", "user_1_sub_2_dev_3"])
    managed = [r for r in cfg["routing"]["rules"] if r.get("ruleTag") == proxy.TAG]
    assert len(managed) == 2 and managed[0]["outboundTag"] == "block"
    rule = managed[1]
    assert rule["port"] == "443" and rule["network"] == "tcp"
    assert set(rule["user"]) == {"user_1_sub_2", "user_1_sub_2_dev_3", "user_1_sub_2_cdn", "user_1_sub_2_dev_3_cdn"}
    assert rule["domain"] == gemini_policy.DOMAINS
    assert cfg["routing"]["rules"][:2] == before["routing"]["rules"][:2]
    assert [r for r in cfg["routing"]["rules"] if r.get("ruleTag") != proxy.TAG] == before["routing"]["rules"]
    out = cfg["outbounds"][-1]
    assert out["protocol"] == "http" and out["streamSettings"]["security"] == "tls"
    assert out["streamSettings"]["tlsSettings"] == {"serverName": "dc.decodo.com", "allowInsecure": False}
    saved = copy.deepcopy(cfg)
    gemini_policy.apply_policy(cfg, {"mode": "warp", "ip_version": "ipv6"})
    proxy.apply(cfg, ["user_1_sub_2", "user_1_sub_2_dev_3"])
    assert cfg == saved  # no reload loop when two policies compose
    assert proxy.apply(cfg, [])
    assert cfg == before


@pytest.mark.parametrize(
    "users", [[""], ["*"], ["user_1"], ["user_1_sub_2_cdn"], ["user_1_sub_2", "anyone@example.com"]]
)
def test_rejects_wildcards_and_arbitrary_users(users):
    with pytest.raises(ValueError):
        DesiredGeminiAdminProxy(kind="gemini_admin_proxy", users=users)


def test_empty_policy_noop():
    cfg = {"inbounds": []}
    before = copy.deepcopy(cfg)
    assert not proxy.apply(cfg, [])
    assert cfg == before


async def test_expired_clients_never_remain_in_personal_proxy(tmp_path, monkeypatch, secret):
    await _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(proxy, "USERS_PATH", tmp_path / "admin-users.json")
    monkeypatch.setattr(gemini_policy, "POLICY_PATH", tmp_path / "policy.json")
    cfg = _with_warp(_config())
    gemini_policy.apply_policy(cfg, {"mode": "warp", "ip_version": "ipv6"})
    monkeypatch.setattr(reconcile, "get_xray_config", AsyncMock(return_value=cfg))
    snap = _snapshot()
    snap.items.append(
        DesiredGeminiAdminProxy(kind="gemini_admin_proxy", users=["user_1_sub_1", "user_9_sub_9"], port=10006)
    )
    install = AsyncMock()
    monkeypatch.setattr(reconcile, "_install_gemini_config", install)
    await reconcile.reconcile_snapshot(snap, observe=False, now_ms=1)
    assert install.await_args.args[3:] == (["user_1_sub_1"], 10006)


def test_decodo_slot_only_changes_port(secret, tmp_path, monkeypatch):
    cfg = _with_warp(_config())
    gemini_policy.apply_policy(cfg, {"mode": "warp", "ip_version": "ipv6"})
    users = ["user_1_sub_2"]
    proxy.apply(cfg, users)
    before = copy.deepcopy(cfg)
    assert proxy.apply(cfg, users, 10010)
    before["outbounds"][-1]["settings"]["servers"][0]["port"] = 10010
    assert cfg == before
    assert not proxy.apply(cfg, users, 10010)
    state = tmp_path / "selection"
    monkeypatch.setattr(proxy, "USERS_PATH", state)
    state.write_text('{"users":["user_1_sub_2"],"port":10010}')
    assert proxy.load_selection() == (users, 10010)
    state.write_text('{"users":["user_1_sub_2"]}')
    assert proxy.load_selection() == (users, None)


@pytest.mark.parametrize("port", [10000, 10011, "10001", True])
def test_invalid_decodo_slot(port):
    with pytest.raises(ValueError):
        DesiredGeminiAdminProxy(kind="gemini_admin_proxy", users=[], port=port)


def test_legacy_proxy_item_canonical_shape():
    item = {"kind": "gemini_admin_proxy", "users": ["user_1_sub_2"]}
    assert DesiredGeminiAdminProxy.model_validate(item).model_dump(mode="json") == item
