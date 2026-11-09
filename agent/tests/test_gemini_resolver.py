import os
import re
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_warp_strategy(monkeypatch):
    monkeypatch.delenv("WARP_DOMAIN_STRATEGY", raising=False)


AGENT = Path(__file__).resolve().parents[1]
RESOLVER = "https+local://dns.comss.one/dns-query"


def _ensure_gemini_resolver():
    src = (AGENT / "entrypoint.sh").read_text()
    domains = re.search(r"^GEMINI_DOMAINS = (\[.*?^\])", src, re.S | re.M)
    fn = re.search(r"(def ensure_gemini_resolver\(cfg: dict\) -> None:.*?)\n\n\n", src, re.S)
    assert domains and fn, "ensure_gemini_resolver not found in entrypoint.sh"
    scope: dict = {"os": os}
    exec(f"GEMINI_DOMAINS = {domains.group(1)}", scope)
    exec(fn.group(1), scope)
    return scope["ensure_gemini_resolver"], scope["GEMINI_DOMAINS"]


def _config() -> dict:
    return {
        "dns": {"servers": ["https+local://9.9.9.9/dns-query"]},
        "inbounds": [
            {"tag": "vless-in", "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}},
            {"tag": "reality-split", "sniffing": {"enabled": True, "destOverride": ["tls"], "routeOnly": True}},
            {"tag": "api"},
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
        "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
    }


def test_gemini_resolves_and_leaves_through_the_resolver(monkeypatch):
    ensure, domains = _ensure_gemini_resolver()
    monkeypatch.delenv("GEMINI_EGRESS", raising=False)
    monkeypatch.setenv("GEMINI_RESOLVER", RESOLVER)
    monkeypatch.delenv("GEMINI_RESOLVER_SKIP_IPS", raising=False)
    config = _config()
    ensure(config)
    ensure(config)  # idempotent across restarts

    assert config["dns"]["servers"] == [
        {"address": RESOLVER, "domains": domains, "skipFallback": True},
        "https+local://9.9.9.9/dns-query",
    ]
    gemini = [o for o in config["outbounds"] if o["tag"] == "gemini"]
    assert gemini == [{"tag": "gemini", "protocol": "freedom", "settings": {"domainStrategy": "ForceIPv4"}}]
    rules = config["routing"]["rules"]
    # Right after the api rule: QUIC to Gemini is refused, TCP goes to the proxy.
    assert rules[1] == {"type": "field", "network": "udp", "domain": domains, "outboundTag": "block"}
    assert rules[2] == {"type": "field", "domain": domains, "outboundTag": "gemini"}
    assert len(rules) == 3
    sniffing = {i["tag"]: i.get("sniffing") for i in config["inbounds"]}
    assert sniffing["vless-in"]["destOverride"] == ["http", "tls", "quic"]
    assert sniffing["reality-split"]["destOverride"] == ["tls"]


def test_dead_proxies_are_dropped_from_answers(monkeypatch):
    ensure, _domains = _ensure_gemini_resolver()
    monkeypatch.delenv("GEMINI_EGRESS", raising=False)
    monkeypatch.setenv("GEMINI_RESOLVER", RESOLVER)
    monkeypatch.setenv("GEMINI_RESOLVER_SKIP_IPS", "159.194.200.33, 203.0.113.0/24")
    config = _config()
    ensure(config)
    assert config["dns"]["servers"][0]["unexpectedIPs"] == ["159.194.200.33", "203.0.113.0/24"]


def _with_warp(config: dict) -> dict:
    config["outbounds"].insert(1, {"tag": "warp", "protocol": "wireguard", "settings": {"secretKey": "k"}})
    return config


def test_gemini_can_leave_through_warp_over_ipv4_when_explicitly_selected(monkeypatch):
    ensure, domains = _ensure_gemini_resolver()
    monkeypatch.delenv("GEMINI_EXTRA_DOMAINS", raising=False)
    monkeypatch.setenv("GEMINI_EGRESS", "warp")
    monkeypatch.setenv("WARP_DOMAIN_STRATEGY", "ForceIPv4")
    monkeypatch.setenv("GEMINI_RESOLVER", RESOLVER)  # WARP wins over a resolver
    config = _with_warp(_config())
    ensure(config)
    ensure(config)

    warp = next(o for o in config["outbounds"] if o["tag"] == "warp")
    assert warp["settings"]["domainStrategy"] == "ForceIPv4"
    assert not any(o["tag"] == "gemini" for o in config["outbounds"])
    assert config["dns"]["servers"] == ["https+local://9.9.9.9/dns-query"]
    assert config["routing"]["rules"][2] == {"type": "field", "domain": domains, "outboundTag": "warp"}

    monkeypatch.delenv("GEMINI_EGRESS")
    monkeypatch.delenv("GEMINI_RESOLVER")
    monkeypatch.delenv("WARP_DOMAIN_STRATEGY")
    ensure(config)
    assert config == _with_warp(_config())


def test_extra_domains_take_the_same_way_out(monkeypatch):
    ensure, domains = _ensure_gemini_resolver()
    monkeypatch.setenv("GEMINI_EGRESS", "warp")
    monkeypatch.delenv("GEMINI_RESOLVER", raising=False)
    monkeypatch.setenv("GEMINI_EXTRA_DOMAINS", "domain:google.com, domain:googleapis.com,domain:gemini.google.com")
    config = _with_warp(_config())
    ensure(config)
    ensure(config)
    expected = [*domains, "domain:google.com", "domain:googleapis.com"]
    assert config["routing"]["rules"][1:3] == [
        {"type": "field", "network": "udp", "domain": expected, "outboundTag": "block"},
        {"type": "field", "domain": expected, "outboundTag": "warp"},
    ]
    assert config["dns"]["queryStrategy"] == "UseIP"
    monkeypatch.delenv("GEMINI_EGRESS")
    monkeypatch.delenv("GEMINI_EXTRA_DOMAINS")
    ensure(config)
    expected_config = _with_warp(_config())
    expected_config["dns"]["queryStrategy"] = "UseIPv4"
    assert config == expected_config


@pytest.mark.parametrize("explicit_strategy", [None, "ForceIPv6"])
def test_gemini_warp_uses_ipv6_without_native_or_ipv4_fallback(monkeypatch, explicit_strategy):
    ensure, domains = _ensure_gemini_resolver()
    monkeypatch.setenv("GEMINI_EGRESS", "warp")
    monkeypatch.delenv("GEMINI_RESOLVER", raising=False)
    monkeypatch.delenv("GEMINI_EXTRA_DOMAINS", raising=False)
    if explicit_strategy:
        monkeypatch.setenv("WARP_DOMAIN_STRATEGY", explicit_strategy)
    config = _with_warp(_config())
    config["dns"]["queryStrategy"] = "UseIPv4"
    ensure(config)
    ensure(config)
    warp = next(o for o in config["outbounds"] if o["tag"] == "warp")
    assert warp["settings"]["domainStrategy"] == "ForceIPv6"
    assert config["dns"]["queryStrategy"] == "UseIP"
    assert not any(o["tag"] == "gemini" for o in config["outbounds"])
    assert config["routing"]["rules"][2] == {"type": "field", "domain": domains, "outboundTag": "warp"}


def test_warp_ipv6_policy_also_applies_without_gemini_override(monkeypatch):
    ensure, _ = _ensure_gemini_resolver()
    monkeypatch.delenv("GEMINI_EGRESS", raising=False)
    monkeypatch.delenv("GEMINI_RESOLVER", raising=False)
    monkeypatch.setenv("WARP_DOMAIN_STRATEGY", "ForceIPv6")
    config = _with_warp(_config())
    ensure(config)
    assert next(o for o in config["outbounds"] if o["tag"] == "warp")["settings"]["domainStrategy"] == "ForceIPv6"
    assert config["dns"]["queryStrategy"] == "UseIP"


def test_invalid_warp_family_is_rejected_instead_of_falling_back(monkeypatch):
    ensure, _ = _ensure_gemini_resolver()
    monkeypatch.setenv("GEMINI_EGRESS", "warp")
    monkeypatch.setenv("WARP_DOMAIN_STRATEGY", "PreferIPv6")
    with pytest.raises(ValueError, match="WARP_DOMAIN_STRATEGY"):
        ensure(_with_warp(_config()))


def test_gemini_can_leave_from_one_address_family_of_the_node(monkeypatch):
    ensure, domains = _ensure_gemini_resolver()
    monkeypatch.setenv("GEMINI_RESOLVER", RESOLVER)  # an address family wins over a resolver
    monkeypatch.delenv("GEMINI_EXTRA_DOMAINS", raising=False)
    for egress, strategy in (("ipv6", "ForceIPv6"), ("ipv4", "ForceIPv4")):
        monkeypatch.setenv("GEMINI_EGRESS", egress)
        config = _with_warp(_config())
        ensure(config)
        ensure(config)
        assert [o for o in config["outbounds"] if o["tag"] == "gemini"] == [
            {"tag": "gemini", "protocol": "freedom", "settings": {"domainStrategy": strategy}}
        ]
        assert config["dns"]["servers"] == ["https+local://9.9.9.9/dns-query"]
        assert config["routing"]["rules"][2] == {"type": "field", "domain": domains, "outboundTag": "gemini"}
        assert "domainStrategy" not in next(o for o in config["outbounds"] if o["tag"] == "warp")["settings"]
        # The global IPv4-only strategy would veto the IPv6 lookups.
        assert config["dns"].get("queryStrategy") == ("UseIP" if egress == "ipv6" else None)


def test_leaving_ipv6_mode_restores_ipv4_only_resolution(monkeypatch):
    ensure, _domains = _ensure_gemini_resolver()
    monkeypatch.delenv("GEMINI_RESOLVER", raising=False)
    monkeypatch.setenv("GEMINI_EGRESS", "ipv6")
    config = _config()
    config["dns"]["queryStrategy"] = "UseIPv4"
    ensure(config)
    assert config["dns"]["queryStrategy"] == "UseIP"
    monkeypatch.delenv("GEMINI_EGRESS")
    ensure(config)
    assert config["dns"]["queryStrategy"] == "UseIPv4"


def test_warp_egress_without_a_warp_outbound_changes_nothing(monkeypatch):
    ensure, _domains = _ensure_gemini_resolver()
    monkeypatch.setenv("GEMINI_EGRESS", "warp")
    monkeypatch.delenv("GEMINI_RESOLVER", raising=False)
    config = _config()
    ensure(config)
    assert config == _config()


def test_a_boot_clears_what_an_older_domain_list_added(monkeypatch):
    ensure, domains = _ensure_gemini_resolver()
    monkeypatch.delenv("GEMINI_EGRESS", raising=False)
    monkeypatch.setenv("GEMINI_RESOLVER", RESOLVER)
    old_list = ["geosite:google-gemini", "domain:gemini.google.com"]
    config = _config()
    config["dns"]["servers"].insert(0, {"address": RESOLVER, "domains": old_list, "skipFallback": True})
    config["routing"]["rules"][1:1] = [
        {"type": "field", "network": "udp", "domain": old_list, "outboundTag": "block"},
        {"type": "field", "domain": old_list, "outboundTag": "gemini"},
    ]
    ensure(config)
    assert [s for s in config["dns"]["servers"] if isinstance(s, dict)] == [
        {"address": RESOLVER, "domains": domains, "skipFallback": True}
    ]
    assert [r["domain"] for r in config["routing"]["rules"] if "domain" in r] == [domains, domains]


def test_without_a_resolver_everything_is_removed(monkeypatch):
    ensure, _domains = _ensure_gemini_resolver()
    monkeypatch.delenv("GEMINI_EGRESS", raising=False)
    monkeypatch.setenv("GEMINI_RESOLVER", RESOLVER)
    config = _config()
    ensure(config)
    monkeypatch.delenv("GEMINI_RESOLVER")
    ensure(config)
    assert config == _config()
