import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import gemini_policy, reconcile
from app.control_models import DesiredGeminiPolicy
from tests.test_gemini_resolver import _config, _with_warp, _ensure_gemini_resolver


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "warp", "ip_version": "ipv4"},
        {"mode": "warp", "ip_version": "ipv6"},
        {"mode": "dns", "resolver": "https://dns.example.com/dns-query"},
        {"mode": "dns", "resolver": "tls://dns.example.com:853"},
    ],
)
def test_valid_policy_and_wire_model(value):
    assert DesiredGeminiPolicy(kind="gemini_policy", policy=value).policy == value


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "warp", "ip_version": "auto"},
        {"mode": "proxy"},
        {"mode": "dns", "resolver": "http://dns.example.com"},
        {"mode": "dns", "resolver": "tls://dns.example.com/path"},
        {"mode": "dns", "resolver": "https://user:secret@dns.example.com/dns-query"},
        {"mode": "dns", "resolver": "https://127.0.0.1/dns-query"},
        {"mode": "dns", "resolver": "tls://[::1]:853"},
        {"mode": "dns", "resolver": "tls://dns.example.com:0"},
        {"mode": "dns", "resolver": "https://dns.example.com/?token=secret"},
    ],
)
def test_invalid_policy(value):
    with pytest.raises(ValueError):
        gemini_policy.validate_policy(value)


def test_absent_policy_preserves_everything():
    cfg = _with_warp(_config())
    cfg["outbounds"].append({"tag": "gemini", "protocol": "socks", "settings": {"password": "keep"}})
    before = copy.deepcopy(cfg)
    assert not gemini_policy.apply_policy(cfg, None)
    assert cfg == before


@pytest.mark.parametrize("mode", ["ipv4", "ipv6", "doh", "dot"])
def test_apply_replaces_only_gemini_preserves_shared_warp_and_order(monkeypatch, mode):
    ensure, domains = _ensure_gemini_resolver()
    cfg = _with_warp(_config())
    monkeypatch.setenv("GEMINI_EGRESS", "warp")
    monkeypatch.setenv("WARP_DOMAIN_STRATEGY", "ForceIPv6")
    ensure(cfg)
    cfg["routing"]["rules"].insert(1, {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"})
    cfg["outbounds"].append(
        {
            "tag": "microsoft-warp-ipv4",
            "protocol": "freedom",
            "settings": {"domainStrategy": "ForceIPv4"},
            "streamSettings": {"sockopt": {"dialerProxy": "warp"}},
        }
    )
    cfg["routing"]["rules"].extend(
        [
            {"ruleTag": "github-direct", "domain": ["geosite:github"], "outboundTag": "direct"},
            {"domain": ["geosite:microsoft"], "outboundTag": "microsoft-warp-ipv4"},
            {"inboundTag": ["gemini-de-relay"], "outboundTag": "gemini-relay-warp"},
        ]
    )
    original = copy.deepcopy(cfg)
    policy = (
        {"mode": "warp", "ip_version": mode}
        if mode.startswith("ipv")
        else {
            "mode": "dns",
            "resolver": "https://dns.example.com/dns-query" if mode == "doh" else "tls://dns.example.com:853",
        }
    )
    assert gemini_policy.apply_policy(cfg, policy)
    assert not gemini_policy.apply_policy(cfg, policy)
    assert [o for o in cfg["outbounds"] if o["tag"] != "gemini"] == original["outbounds"]
    assert cfg["routing"]["rules"][:2] == original["routing"]["rules"][:2]
    assert cfg["routing"]["rules"][4:] == original["routing"]["rules"][4:]
    assert set(gemini_policy.DOMAINS) == set(domains)
    outbound = next(o for o in cfg["outbounds"] if o["tag"] == "gemini")
    if mode.startswith("ipv"):
        assert outbound["streamSettings"]["sockopt"]["dialerProxy"] == "warp"
        assert outbound["settings"]["domainStrategy"] == ("ForceIPv4" if mode == "ipv4" else "ForceIPv6")
    else:
        assert "streamSettings" not in outbound
        assert cfg["dns"]["disableFallbackIfMatch"]
        assert cfg["dns"]["servers"][0]["address"] == (
            "https+local://dns.example.com/dns-query" if mode == "doh" else "tcp+local://127.0.0.1:18553"
        )


def test_missing_warp_is_atomic_failure():
    cfg = _config()
    before = copy.deepcopy(cfg)
    with pytest.raises(ValueError, match="WARP"):
        gemini_policy.apply_policy(cfg, {"mode": "warp", "ip_version": "ipv4"})
    assert cfg == before


def test_persisted_policy_load_and_corruption(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    monkeypatch.setattr(gemini_policy, "POLICY_PATH", path)
    assert gemini_policy.load_policy() is None
    path.write_text(json.dumps({"mode": "warp", "ip_version": "ipv4"}))
    assert gemini_policy.load_policy()["ip_version"] == "ipv4"
    path.write_text("{bad")
    with pytest.raises(ValueError):
        gemini_policy.load_policy()


async def test_rejected_candidate_never_replaces_live_config(tmp_path, monkeypatch):
    monkeypatch.setattr(reconcile.settings, "xray_config_path", str(tmp_path / "xray.json"))
    proc = type("Proc", (), {"wait": AsyncMock(return_value=1)})()
    monkeypatch.setattr(reconcile.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    save = AsyncMock()
    monkeypatch.setattr(reconcile, "save_xray_config", save)
    with pytest.raises(reconcile.ReconcileError, match="rejected"):
        await reconcile._install_gemini_config({}, {}, {"mode": "warp", "ip_version": "ipv4"})
    save.assert_not_called()
    assert not list(tmp_path.iterdir())


async def test_runtime_failure_restores_config_and_policy(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    monkeypatch.setattr(gemini_policy, "POLICY_PATH", path)
    old = {"mode": "dns", "resolver": "https://dns.example.com/dns-query"}
    path.write_text(json.dumps(old))
    monkeypatch.setattr(reconcile.settings, "xray_config_path", str(tmp_path / "xray.json"))
    proc = type("Proc", (), {"wait": AsyncMock(return_value=0)})()
    monkeypatch.setattr(reconcile.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    save = AsyncMock()
    monkeypatch.setattr(reconcile, "save_xray_config", save)
    monkeypatch.setattr(reconcile, "reload_xray", lambda: None)
    monkeypatch.setattr(reconcile, "wait_for_xray_ready", AsyncMock(side_effect=[False, True]))
    with pytest.raises(reconcile.ReconcileError, match="readiness"):
        await reconcile._install_gemini_config({"new": 1}, {"old": 1}, {"mode": "warp", "ip_version": "ipv4"})
    assert gemini_policy.load_policy() == old
    assert [call.args[0] for call in save.call_args_list] == [{"new": 1}, {"old": 1}]
