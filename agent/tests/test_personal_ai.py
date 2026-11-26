import copy
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from app import gemini_admin_proxy, gemini_policy, personal_ai


@pytest.fixture
def baseline():
    return {
        "inbounds": [
            {"tag": "client-xhttp", "protocol": "vless"},
            {"tag": "client-cdn", "protocol": "vless"},
            {"tag": "api", "protocol": "dokodemo-door"},
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
        "routing": {
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
            ]
        },
    }


def test_only_the_pinned_subscription_and_ai_hosts(baseline, monkeypatch):
    dns = {"mode": "dns", "resolver": "https://dns.example.com/dns-query"}
    gemini_policy.apply_policy(baseline, dns)
    monkeypatch.setattr(
        gemini_admin_proxy,
        "credentials",
        lambda: {"host": "dc.decodo.com", "port": 10001, "username": "test", "password": "test"},
    )
    gemini_admin_proxy.apply(baseline, ["user_12_sub_36"])
    config = copy.deepcopy(baseline)
    p = personal_ai.from_url("socks5://test:encoded%40pass@1.1.1.1:10001", 12, 36)
    assert personal_ai.apply(config, p)
    assert not personal_ai.apply(config, p)
    rules = config["routing"]["rules"]
    udp, tcp = [r for r in rules if r.get("ruleTag") == personal_ai.TAG]
    assert rules[:2] == baseline["routing"]["rules"][:2]
    assert [r for r in rules if r.get("ruleTag") != personal_ai.TAG] == baseline["routing"]["rules"]
    assert rules.index(tcp) < next(i for i, r in enumerate(rules) if r.get("ruleTag") == gemini_admin_proxy.TAG)
    assert udp["network"] == "udp" and udp["outboundTag"] == "block" and tcp["network"] == "tcp"
    assert udp["port"] == tcp["port"] == "443"
    assert tcp["inboundTag"] == ["client-cdn", "client-xhttp"]
    rx = re.compile(tcp["user"][0].removeprefix("regexp:"))
    assert all(rx.fullmatch(s) for s in ("user_12_sub_36", "user_12_sub_36_dev_10", "user_12_sub_36_dev_43_cdn"))
    assert not any(
        rx.search(s)
        for s in (
            "user_12_sub_367",
            "user_121_sub_36",
            "user_12_sub_35_dev_10",
            "user_12_sub_36_other",
            "user_45_sub_36",
        )
    )
    assert {
        "domain:chatgpt.com",
        "domain:claude.ai",
        "domain:anthropic.com",
        "full:aistudio.google.com",
        "domain:grok.com",
        "domain:meta.ai",
    } <= set(tcp["domain"])
    assert not {"domain:google.com", "domain:x.com", "domain:facebook.com"} & set(tcp["domain"])
    out = config["outbounds"][-1]
    assert out["protocol"] == "socks"
    assert out["settings"]["servers"][0]["users"][0]["pass"] == "encoded@pass"
    gemini_policy.apply_policy(config, dns)
    gemini_admin_proxy.apply(config, ["user_12_sub_36"])
    rules = config["routing"]["rules"]
    assert rules.index(tcp) < next(i for i, r in enumerate(rules) if r.get("ruleTag") == gemini_admin_proxy.TAG)
    assert personal_ai.apply(config, None) and config == baseline


def test_catalog_update_only_changes_owned_domains(baseline):
    gemini_policy.apply_policy(baseline, {"mode": "dns", "resolver": "https://dns.example.com/dns-query"})
    p = personal_ai.from_url("socks5://test:pass@1.1.1.1:10001", 12, 36)
    personal_ai.apply(baseline, p)
    # Gemini reapplication can move its named outbound after the ISP outbound.
    baseline["outbounds"].append(baseline["outbounds"].pop(-2))
    prior = copy.deepcopy(baseline)
    for r in prior["routing"]["rules"]:
        if r.get("ruleTag") == personal_ai.TAG:
            r["domain"] = ["full:console.anthropic.com", "full:api.anthropic.com"]
    assert personal_ai.apply(prior, p)
    assert prior == baseline


@pytest.mark.parametrize(
    "raw",
    ["http://t:p@1.1.1.1:443", "socks5://t:p@127.0.0.1:443", "socks5://t:p@1.1.1.1:443/q", "socks5://t:%0A@8.8.8.8:53"],
)
def test_rejects_bad_secrets(raw):
    with pytest.raises(ValueError, match="valid SOCKS5"):
        personal_ai.from_url(raw, 12, 36)


def test_xray_only_reloads_child_without_exiting_namespace(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    script = tmp_path / "xray"
    marker = tmp_path / "pids"
    script.write_text('#!/bin/sh\necho "$$" >> "$PID_OUTPUT"\nexec sleep 90\n')
    script.chmod(0o755)
    env = {
        **os.environ,
        "XRAY_RUN_MODE": "xray-only",
        "XRAY_CONFIG_PATH": str(cfg),
        "PATH": str(tmp_path) + ":" + os.environ["PATH"],
        "PID_OUTPUT": str(marker),
    }
    process = subprocess.Popen(
        ["bash", str(Path(__file__).resolve().parents[1] / "entrypoint.sh")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def pids(n):
        for _ in range(80):
            lines = marker.read_text().splitlines() if marker.exists() else []
            if len(lines) >= n:
                return [int(p) for p in lines]
            assert process.poll() is None
            time.sleep(0.025)
        raise AssertionError("child start not observed")

    try:
        first = pids(1)[0]
        os.kill(first, signal.SIGHUP)
        assert pids(2)[-1] != first and process.poll() is None
        process.terminate()
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
