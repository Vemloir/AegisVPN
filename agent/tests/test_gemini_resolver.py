import os
import re
from pathlib import Path

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


def test_gemini_can_leave_through_warp_over_ipv4(monkeypatch):
    ensure, domains = _ensure_gemini_resolver()
    monkeypatch.setenv("GEMINI_EGRESS", "warp")
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
    ensure(config)
    assert config == _with_warp(_config())


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
