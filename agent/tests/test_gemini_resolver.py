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
    monkeypatch.setenv("GEMINI_RESOLVER", RESOLVER)
    monkeypatch.setenv("GEMINI_RESOLVER_SKIP_IPS", "159.194.200.33, 203.0.113.0/24")
    config = _config()
    ensure(config)
    assert config["dns"]["servers"][0]["unexpectedIPs"] == ["159.194.200.33", "203.0.113.0/24"]


def test_without_a_resolver_everything_is_removed(monkeypatch):
    ensure, _domains = _ensure_gemini_resolver()
    monkeypatch.setenv("GEMINI_RESOLVER", RESOLVER)
    config = _config()
    ensure(config)
    monkeypatch.delenv("GEMINI_RESOLVER")
    ensure(config)
    assert config == _config()
