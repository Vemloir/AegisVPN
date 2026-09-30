import json
import re
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]


def _entrypoint_fn(name: str):
    src = (AGENT / "entrypoint.sh").read_text()
    fn = re.search(rf"(def {name}\(cfg: dict\) -> None:.*?)\n\n\n(?=def |ensure_warp)", src, re.S)
    assert fn, f"{name} not found in entrypoint.sh"
    scope: dict = {"os": __import__("os")}
    exec(fn.group(1), scope)
    return scope[name]


def _ensure_private_blocked():
    return _entrypoint_fn("ensure_private_blocked")


def _legacy_config() -> dict:
    return {
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "domain": ["geosite:category-games"], "outboundTag": "direct"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
                {"type": "field", "domain": ["geosite:private"], "outboundTag": "direct"},
                {"type": "field", "network": "tcp,udp", "outboundTag": "direct"},
            ],
        }
    }


def test_private_destinations_are_blocked_before_anything_else():
    cfg = _legacy_config()
    _ensure_private_blocked()(cfg)
    rules = cfg["routing"]["rules"]
    assert rules[0]["outboundTag"] == "api"
    assert rules[1] == {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"}
    assert rules[2] == {"type": "field", "domain": ["geosite:private"], "outboundTag": "block"}
    # No rule anywhere may still send private destinations out of the node.
    assert not [r for r in rules[3:] if "geoip:private" in r.get("ip", []) or "geosite:private" in r.get("domain", [])]
    # A public name resolving to 127.0.0.1 must hit the IP rule, not pass as a domain.
    assert cfg["routing"]["domainStrategy"] == "IPOnDemand"


def test_private_block_is_idempotent():
    fn = _ensure_private_blocked()
    once = _legacy_config()
    fn(once)
    twice = json.loads(json.dumps(once))
    fn(twice)
    assert once == twice


def test_template_ships_with_private_blocked():
    template = json.loads((AGENT / "template.json").read_text())
    rules = template["routing"]["rules"]
    assert template["routing"]["domainStrategy"] == "IPOnDemand"
    assert rules[1]["ip"] == ["geoip:private"] and rules[1]["outboundTag"] == "block"
    assert rules[2]["domain"] == ["geosite:private"] and rules[2]["outboundTag"] == "block"


def test_lb_probe_rule_precedes_the_private_block(monkeypatch):
    monkeypatch.delenv("LB_PROBE_PORT", raising=False)
    cfg = _legacy_config()
    cfg["outbounds"] = [{"tag": "direct", "protocol": "freedom"}]
    _ensure_private_blocked()(cfg)
    ensure_lb_probe = _entrypoint_fn("ensure_lb_probe")
    ensure_lb_probe(cfg)
    ensure_lb_probe(cfg)  # idempotent
    rules = cfg["routing"]["rules"]
    assert rules[1] == {"type": "field", "domain": ["full:lb.aegisvpn.org"], "port": "80", "outboundTag": "lb-probe"}
    assert rules[2]["ip"] == ["geoip:private"] and rules[2]["outboundTag"] == "block"
    probes = [o for o in cfg["outbounds"] if o["tag"] == "lb-probe"]
    assert probes == [{"tag": "lb-probe", "protocol": "freedom", "settings": {"redirect": "127.0.0.1:10086"}}]
    assert cfg["outbounds"][0]["tag"] == "direct"  # default outbound unchanged
