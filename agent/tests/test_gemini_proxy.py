import os
import re
from pathlib import Path

import pytest


def _ensure():
    source = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()
    domains = re.search(r"^GEMINI_DOMAINS = (\[.*?^\])", source, re.S | re.M)
    function = re.search(r"(def ensure_gemini_resolver\(cfg: dict\) -> None:.*?)\n\n\n", source, re.S)
    scope = {"os": os}
    exec(f"GEMINI_DOMAINS = {domains.group(1)}\n{function.group(1)}", scope)
    return scope["ensure_gemini_resolver"]


@pytest.fixture
def proxy_env(monkeypatch):
    monkeypatch.setenv("GEMINI_EGRESS", "proxy")
    monkeypatch.setenv("WARP_DOMAIN_STRATEGY", "ForceIPv6")
    values = {"HOST": "192.0.2.22", "PORT": "24443", "USER": "relay", "PASSWORD": "test-only-password",
              "SERVER_NAME": "gemini-relay.internal", "CA_FILE": "/data/gemini-relay/ca.crt"}
    for key, value in values.items():
        monkeypatch.setenv("GEMINI_PROXY_" + key, value)


def _config():
    return {"dns": {"servers": ["https+local://9.9.9.9/dns-query"], "queryStrategy": "UseIP"},
            "outbounds": [{"tag": "direct", "protocol": "freedom"},
                          {"tag": "warp", "protocol": "wireguard", "settings": {"domainStrategy": "ForceIPv6"}}],
            "inbounds": [], "routing": {"rules": []}}


def test_proxy_is_authenticated_verified_tls_and_idempotent(proxy_env):
    config = _config()
    ensure = _ensure()
    ensure(config)
    ensure(config)
    proxies = [outbound for outbound in config["outbounds"] if outbound["tag"] == "gemini"]
    assert len(proxies) == 1 and proxies[0]["protocol"] == "socks"
    assert proxies[0]["settings"]["servers"] == [{"address": "192.0.2.22", "port": 24443,
                                                "users": [{"user": "relay", "pass": "test-only-password"}]}]
    assert proxies[0]["streamSettings"] == {"network": "tcp", "security": "tls", "tlsSettings": {
        "serverName": "gemini-relay.internal", "disableSystemRoot": True,
        "certificates": [{"certificateFile": "/data/gemini-relay/ca.crt", "usage": "verify"}],
    }}
    assert config["outbounds"][:2] == _config()["outbounds"]
    assert config["dns"] == _config()["dns"]
    for rule in config["routing"]["rules"]:
        assert "domain:gemini.google.com" in rule["domain"]
    assert any(rule.get("network") == "udp" and rule["outboundTag"] == "block" for rule in config["routing"]["rules"])


@pytest.mark.parametrize("missing", ["HOST", "PORT", "USER", "PASSWORD", "SERVER_NAME", "CA_FILE"])
def test_missing_proxy_settings_fail_closed(proxy_env, monkeypatch, missing):
    monkeypatch.delenv("GEMINI_PROXY_" + missing)
    with pytest.raises(ValueError, match="GEMINI_EGRESS=proxy requires"):
        _ensure()(_config())


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_invalid_proxy_port_fails_closed(proxy_env, monkeypatch, port):
    monkeypatch.setenv("GEMINI_PROXY_PORT", port)
    with pytest.raises(ValueError):
        _ensure()(_config())
