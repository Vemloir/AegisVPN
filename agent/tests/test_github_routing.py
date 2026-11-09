import copy
import os
import re
from pathlib import Path


AGENT = Path(__file__).resolve().parents[1]


def _entrypoint_fn(name):
    source = (AGENT / "entrypoint.sh").read_text()
    match = re.search(rf"^(def {name}\(cfg: dict\) -> None:.*?)\n\n\ndef ", source, re.S | re.M)
    assert match, f"{name} not found"
    namespace = {"os": os}
    exec(match.group(1), namespace)
    return namespace[name]


def _config():
    return {
        "routing": {
            "domainStrategy": "IPOnDemand",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
                {"type": "field", "domain": ["geosite:private"], "outboundTag": "block"},
                {"type": "field", "domain": ["geosite:google-gemini"], "outboundTag": "warp"},
                {"type": "field", "domain": ["geosite:category-games"], "outboundTag": "direct"},
                {"type": "field", "domain": ["geosite:microsoft"], "outboundTag": "warp"},
                {"type": "field", "domain": ["geosite:category-ads-all"], "outboundTag": "block"},
            ],
        },
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "warp", "protocol": "wireguard", "settings": {"secretKey": "unchanged", "domainStrategy": "ForceIPv6"}},
            {"tag": "block", "protocol": "blackhole"},
        ],
    }


def test_github_precedes_microsoft_without_changing_other_routes_or_outbounds():
    config = _config()
    before = copy.deepcopy(config)
    _entrypoint_fn("ensure_github_direct")(config)
    rules = config["routing"]["rules"]
    assert rules[5] == {
        "type": "field", "domain": ["geosite:github"],
        "outboundTag": "direct", "ruleTag": "github-direct",
    }
    assert [rule for rule in rules if rule.get("ruleTag") != "github-direct"] == before["routing"]["rules"]
    assert config["outbounds"] == before["outbounds"]
    assert config["routing"]["domainStrategy"] == "IPOnDemand"


def test_github_exception_is_idempotent():
    config = _config()
    ensure = _entrypoint_fn("ensure_github_direct")
    ensure(config)
    before = copy.deepcopy(config)
    ensure(config)
    assert config == before


def test_no_exception_without_microsoft_warp_rule():
    config = _config()
    config["routing"]["rules"] = [rule for rule in config["routing"]["rules"] if "geosite:microsoft" not in rule.get("domain", [])]
    before = copy.deepcopy(config)
    _entrypoint_fn("ensure_github_direct")(config)
    assert config == before


def test_stale_owned_exception_is_removed_when_microsoft_warp_rule_is_removed():
    config = _config()
    ensure = _entrypoint_fn("ensure_github_direct")
    ensure(config)
    config["routing"]["rules"] = [rule for rule in config["routing"]["rules"] if "geosite:microsoft" not in rule.get("domain", [])]
    ensure(config)
    assert not any(rule.get("ruleTag") == "github-direct" for rule in config["routing"]["rules"])


def test_private_guards_precede_github_in_boot_order():
    config = _config()
    config["routing"]["rules"] = [rule for rule in config["routing"]["rules"] if rule.get("outboundTag") != "block"]
    _entrypoint_fn("ensure_github_direct")(config)
    _entrypoint_fn("ensure_private_blocked")(config)
    rules = config["routing"]["rules"]
    github_index = next(index for index, rule in enumerate(rules) if rule.get("ruleTag") == "github-direct")
    for index, rule in enumerate(rules):
        if "geoip:private" in rule.get("ip", []) or "geosite:private" in rule.get("domain", []):
            assert index < github_index and rule["outboundTag"] == "block"


def test_entrypoint_calls_github_exception_after_warp_and_before_private_guards():
    source = (AGENT / "entrypoint.sh").read_text()
    assert source.index("\nensure_warp(config)\n") < source.index("\nensure_github_direct(config)\n") < source.index("\nensure_private_blocked(config)\n")
