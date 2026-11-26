import ast
import copy
import os
from pathlib import Path

import pytest


AGENT = Path(__file__).resolve().parents[1]
TAG = "microsoft-warp-ipv4"


def _functions():
    source = (AGENT / "entrypoint.sh").read_text()
    block = source[source.index("def ensure_github_direct("):source.index("\nensure_warp(config)\n")]
    tree = ast.parse(block)
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "GEMINI_DOMAINS" for target in node.targets)
    ]
    namespace = {"os": os}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "entrypoint-functions", "exec"), namespace)
    return namespace


def _config():
    return {
        "dns": {"servers": ["https+local://9.9.9.9/dns-query"], "queryStrategy": "UseIP"},
        "inbounds": [],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "warp", "protocol": "wireguard", "settings": {"secretKey": "test-only", "domainStrategy": "ForceIPv6"}},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"rules": [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            {"type": "field", "domain": ["geosite:google-gemini"], "outboundTag": "warp"},
            {"type": "field", "domain": ["geosite:microsoft"], "outboundTag": "warp"},
        ]},
    }


@pytest.mark.parametrize("strategy", ["ForceIPv4", "ForceIPv6"])
def test_microsoft_resolves_ipv4_then_chains_into_unchanged_warp(strategy):
    config = _config()
    config["outbounds"][1]["settings"]["domainStrategy"] = strategy
    before = copy.deepcopy(config)
    _functions()["ensure_microsoft_warp_ipv4"](config)
    adapter = next(outbound for outbound in config["outbounds"] if outbound["tag"] == TAG)
    assert adapter == {
        "tag": TAG, "protocol": "freedom",
        "settings": {"domainStrategy": "ForceIPv4"},
        "streamSettings": {"sockopt": {"dialerProxy": "warp"}},
    }
    assert [outbound for outbound in config["outbounds"] if outbound["tag"] != TAG] == before["outbounds"]
    assert sum(outbound["protocol"] == "wireguard" for outbound in config["outbounds"]) == 1
    assert config["dns"] == before["dns"]
    assert config["routing"]["rules"][1] == before["routing"]["rules"][1]
    assert config["routing"]["rules"][2] == {"type": "field", "domain": ["geosite:microsoft"], "outboundTag": TAG}


def test_adapter_is_idempotent_and_preserves_direct_default():
    config = _config()
    ensure = _functions()["ensure_microsoft_warp_ipv4"]
    ensure(config)
    before = copy.deepcopy(config)
    ensure(config)
    assert config == before
    assert config["outbounds"][0]["tag"] == "direct"


@pytest.mark.parametrize("missing", ["warp", "microsoft_rule"])
def test_no_adapter_when_warp_or_microsoft_policy_is_absent(missing):
    config = _config()
    if missing == "warp":
        config["outbounds"] = [outbound for outbound in config["outbounds"] if outbound["tag"] != "warp"]
    else:
        config["routing"]["rules"] = [rule for rule in config["routing"]["rules"] if rule.get("domain") != ["geosite:microsoft"]]
    before = copy.deepcopy(config)
    _functions()["ensure_microsoft_warp_ipv4"](config)
    assert config == before


def test_github_exception_survives_microsoft_adapter():
    config = _config()
    functions = _functions()
    functions["ensure_microsoft_warp_ipv4"](config)
    for _ in range(2):
        functions["ensure_github_direct"](config)
    rules = config["routing"]["rules"]
    github = next(index for index, rule in enumerate(rules) if rule.get("ruleTag") == "github-direct")
    microsoft = next(index for index, rule in enumerate(rules) if rule.get("outboundTag") == TAG)
    assert github < microsoft
    assert rules[github]["outboundTag"] == "direct"
    assert sum(rule.get("ruleTag") == "github-direct" for rule in rules) == 1


def test_complete_boot_is_idempotent_with_one_warp_and_private_guards(monkeypatch):
    monkeypatch.delenv("WARP_SECRET_KEY", raising=False)
    monkeypatch.delenv("GEMINI_EXTRA_DOMAINS", raising=False)
    monkeypatch.setenv("GEMINI_EGRESS", "warp")
    monkeypatch.setenv("WARP_DOMAIN_STRATEGY", "ForceIPv6")
    config = _config()
    functions = _functions()
    names = ("ensure_warp", "ensure_github_direct", "ensure_gemini_resolver", "ensure_microsoft_warp_ipv4", "ensure_private_blocked")
    for name in names:
        functions[name](config)
    before = copy.deepcopy(config)
    for name in names:
        functions[name](config)
    assert config == before
    assert sum(outbound["protocol"] == "wireguard" for outbound in config["outbounds"]) == 1
    assert next(outbound for outbound in config["outbounds"] if outbound["tag"] == "warp")["settings"]["domainStrategy"] == "ForceIPv6"
    assert config["dns"]["queryStrategy"] == "UseIP"
    rules = config["routing"]["rules"]
    microsoft = next(index for index, rule in enumerate(rules) if rule.get("outboundTag") == TAG)
    github = next(index for index, rule in enumerate(rules) if rule.get("ruleTag") == "github-direct")
    assert github < microsoft
    assert sum(rule.get("domain") == ["geosite:microsoft"] for rule in rules) == 1
    assert any(rule.get("domain") == functions["GEMINI_DOMAINS"] and rule.get("outboundTag") == "warp" for rule in rules)
    for index, rule in enumerate(rules):
        if "geoip:private" in rule.get("ip", []) or "geosite:private" in rule.get("domain", []):
            assert index < github and rule["outboundTag"] == "block"
