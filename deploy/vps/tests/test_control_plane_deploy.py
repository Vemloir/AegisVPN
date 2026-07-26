import os
import inspect
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from deploy.vps.control_plane import (
    PromotionState,
    ensure_control_ca,
    initialize_control_server,
    issue_node_credentials,
    render_agent_firewall,
    render_node_control_env,
    validate_promotion,
)
from deploy.vps import update as update_script

ROOT = Path(__file__).resolve().parents[3]


def test_node_credentials_are_unique_and_private(tmp_path):
    ca_cert, ca_key = ensure_control_ca(tmp_path / "ca")
    first = issue_node_credentials(
        ca_cert=ca_cert,
        ca_key=ca_key,
        output_dir=tmp_path / "node-one",
        node_name="node-one",
    )
    second = issue_node_credentials(
        ca_cert=ca_cert,
        ca_key=ca_key,
        output_dir=tmp_path / "node-two",
        node_name="node-two",
    )

    assert first.token != second.token
    assert first.token_hash != second.token_hash
    assert first.cert_fingerprint != second.cert_fingerprint
    assert first.token_hash
    assert len(first.cert_fingerprint) == 64
    assert first.ca_cert.read_bytes() == second.ca_cert.read_bytes()
    for private_file in (first.client_key, first.token_file):
        assert os.stat(private_file).st_mode & 0o777 == 0o600


def test_central_material_is_idempotent_and_never_replaces_proxy_secret(tmp_path):
    ca_cert, _ = ensure_control_ca(tmp_path / "ca")
    caddy_template = (
        ROOT / "deploy/vps/control-plane/control.caddy.example"
    )
    output = tmp_path / "server"

    initialize_control_server(
        ca_cert=ca_cert,
        output_dir=output,
        caddy_template=caddy_template,
    )
    first_secret = (output / "proxy-secret").read_bytes()
    initialize_control_server(
        ca_cert=ca_cert,
        output_dir=output,
        caddy_template=caddy_template,
    )

    assert (output / "proxy-secret").read_bytes() == first_secret
    assert len(first_secret) >= 43
    assert os.stat(output / "proxy-secret").st_mode & 0o777 == 0o600
    assert (output / "client-ca.crt").read_bytes() == ca_cert.read_bytes()
    assert (output / "control.caddy").read_bytes() == caddy_template.read_bytes()


def test_promotion_requires_fresh_error_free_matching_generation():
    now = datetime.now(UTC).replace(tzinfo=None)
    ready = PromotionState(
        desired_generation=9,
        applied_generation=9,
        desired_digest="9" * 64,
        applied_digest="9" * 64,
        last_seen_at=now - timedelta(seconds=10),
        last_error=None,
    )
    validate_promotion(ready, now=now, max_age_seconds=90)

    with pytest.raises(ValueError, match="generation"):
        validate_promotion(
            replace(ready, applied_generation=8),
            now=now,
            max_age_seconds=90,
        )
    with pytest.raises(ValueError, match="heartbeat"):
        validate_promotion(
            replace(ready, last_seen_at=now - timedelta(minutes=5)),
            now=now,
            max_age_seconds=90,
        )
    with pytest.raises(ValueError, match="error"):
        validate_promotion(
            replace(ready, last_error="reconcile failed"),
            now=now,
            max_age_seconds=90,
        )


def test_caddy_compose_and_agent_bind_are_private_by_construction():
    caddyfile = (
        ROOT / "deploy/vps/control-plane/control.caddy.example"
    ).read_text()
    compose = (ROOT / "deploy/vps/docker-compose.yml").read_text()
    entrypoint = (ROOT / "agent/entrypoint.sh").read_text()

    assert "client_auth" in caddyfile
    assert "require_and_verify" in caddyfile
    assert "trust_pool file /etc/caddy/control/client-ca.crt" in caddyfile
    assert "{tls_client_fingerprint}" in caddyfile
    assert "{file./etc/caddy/control/proxy-secret}" in caddyfile
    assert "header_up -X-Aegis-Proxy-Secret" not in caddyfile
    assert "header_up -X-Aegis-Node-Fingerprint" not in caddyfile
    agent_service = compose.split("\n  agent:", 1)[1].split("\n  bot:", 1)[0]
    assert "./data/control/node:/data/control:ro" in agent_service
    assert "./data/control/server:/etc/caddy/control:ro" in compose
    assert 'AGENT_BIND_HOST="${AGENT_BIND_HOST:-0.0.0.0}"' in entrypoint
    assert "--host \"$AGENT_BIND_HOST\"" in entrypoint


def test_observe_and_pull_rendering_keeps_data_plane_ports_untouched():
    observe = render_node_control_env(
        control_urls=["https://control.example.com"],
        mode="observe",
    )
    promoted = render_node_control_env(
        control_urls=["https://control.example.com"],
        mode="apply",
        bind_host="127.0.0.1",
    )
    firewall = render_agent_firewall(
        control_server_ip="203.0.113.10",
        public_agent=False,
    )
    rollback = render_agent_firewall(
        control_server_ip="203.0.113.10",
        public_agent=True,
    )

    assert "CONTROL_URLS=https://control.example.com" in observe
    assert "CONTROL_MODE=observe" in observe
    assert "AGENT_BIND_HOST=0.0.0.0" in observe
    assert "CONTROL_MODE=apply" in promoted
    assert "AGENT_BIND_HOST=127.0.0.1" in promoted
    assert "8444" in firewall
    assert "--dport 8444 -s 127.0.0.0/8 -j ACCEPT" in firewall
    assert "--dport 8444 -j DROP" in firewall
    assert "--dport 8444 -s 203.0.113.10 -j ACCEPT" in rollback
    assert "443" not in firewall
    assert "443" not in rollback


def test_control_url_must_use_standard_https_port():
    with pytest.raises(ValueError, match="standard HTTPS"):
        render_node_control_env(
            control_urls=["https://control.example.com:8443"],
            mode="observe",
        )


def test_guarded_promotion_closes_agent_only_after_matching_ack(monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    ready = PromotionState(
        desired_generation=4,
        applied_generation=4,
        desired_digest="a" * 64,
        applied_digest="a" * 64,
        last_seen_at=now,
        last_error=None,
    )
    events: list[tuple] = []
    monkeypatch.setattr(
        update_script,
        "_set_node_control_settings",
        lambda _client, **kwargs: events.append(("node", kwargs)),
    )
    monkeypatch.setattr(
        update_script,
        "_apply_agent_firewall",
        lambda _client, **kwargs: events.append(("firewall", kwargs)),
    )
    monkeypatch.setattr(
        update_script,
        "_fetch_promotion_state",
        lambda _client, **_kwargs: ready,
    )
    monkeypatch.setattr(
        update_script,
        "_set_server_control_mode",
        lambda _client, **kwargs: events.append(("central", kwargs)),
    )

    update_script.promote_pull(
        object(),
        object(),
        server_id=17,
        control_server_ip="203.0.113.10",
        timeout_seconds=5,
    )

    assert events == [
        ("node", {"mode": "apply", "bind_host": "0.0.0.0"}),
        (
            "firewall",
            {
                "control_server_ip": "203.0.113.10",
                "allow_control_server": True,
            },
        ),
        ("node", {"mode": "apply", "bind_host": "127.0.0.1"}),
        (
            "firewall",
            {
                "control_server_ip": "203.0.113.10",
                "allow_control_server": False,
            },
        ),
        ("central", {"server_id": 17, "mode": "pull"}),
    ]
    assert "restart xray" not in inspect.getsource(update_script.promote_pull)


def test_rollback_remains_fixed_ip_only(monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(
        update_script,
        "_set_node_control_settings",
        lambda _client, **kwargs: events.append(("node", kwargs)),
    )
    monkeypatch.setattr(
        update_script,
        "_apply_agent_firewall",
        lambda _client, **kwargs: events.append(("firewall", kwargs)),
    )
    monkeypatch.setattr(
        update_script,
        "_set_server_control_mode",
        lambda _client, **kwargs: events.append(("central", kwargs)),
    )

    update_script.rollback_observe(
        object(),
        object(),
        server_id=17,
        control_server_ip="203.0.113.10",
    )

    assert events[-1] == (
        "firewall",
        {
            "control_server_ip": "203.0.113.10",
            "allow_control_server": True,
        },
    )


def test_control_host_update_recreates_bot_and_siteapi_without_xray():
    source = inspect.getsource(update_script.update_bot)

    assert "docker compose build bot siteapi" in source
    assert "docker compose up -d --no-deps bot siteapi" in source
    assert "aegis-siteapi" in source
    assert "restart xray" not in source


def test_template_uses_stable_policy_routing_and_independent_dns():
    template = json.loads((ROOT / "agent/template.json").read_text())
    level0 = template["policy"]["levels"]["0"]
    routing = template["routing"]

    assert level0["handshake"] == 8
    assert level0["connIdle"] == 300
    assert routing["domainStrategy"] == "AsIs"
    assert not any(
        rule.get("network") == "udp"
        and str(rule.get("port")) == "443"
        and rule.get("outboundTag") == "block"
        for rule in routing["rules"]
    )
    assert not any(
        (
            "geoip:ru" in rule.get("ip", [])
            or "geosite:category-ru" in rule.get("domain", [])
        )
        and rule.get("outboundTag") == "direct"
        for rule in routing["rules"]
    )
    assert template["dns"] == {
        "servers": [
            "https+local://9.9.9.9/dns-query",
            "https+local://1.1.1.1/dns-query",
        ],
        "queryStrategy": "UseIPv4",
        "enableParallelQuery": True,
        "serveStale": True,
        "serveExpiredTTL": 86400,
    }


def test_entrypoint_and_provisioning_keep_idle_at_300():
    entrypoint = (ROOT / "agent/entrypoint.sh").read_text()
    update_source = (ROOT / "deploy/vps/update.py").read_text()

    assert "XRAY_CONN_IDLE=${XRAY_CONN_IDLE:-300}" in entrypoint
    assert 'conn_idle = _int_env("XRAY_CONN_IDLE", 300)' in entrypoint
    assert 'XRAY_CONN_IDLE = 300' in update_source
    assert 'env["XRAY_CONN_IDLE"] = "300"' in update_source
    assert 'env.setdefault("XRAY_CONN_IDLE", "60")' not in update_source


def test_stability_patch_preserves_data_plane_identity_and_clients():
    template = json.loads((ROOT / "agent/template.json").read_text())
    live = deepcopy(template)
    live["policy"]["levels"]["0"].update(
        {
            "handshake": 4,
            "connIdle": 30,
            "statsUserUplink": True,
            "customPolicy": 17,
        }
    )
    live["dns"] = {
        "servers": ["https://9.9.9.9/dns-query"],
        "queryStrategy": "UseIPv4",
    }
    live["routing"]["domainStrategy"] = "IPIfNonMatch"
    live["routing"]["rules"].insert(
        -1,
        {
            "type": "field",
            "domain": ["geosite:google-gemini"],
            "outboundTag": "warp",
        },
    )
    live["inbounds"][0]["settings"]["clients"] = [
        {"id": "uuid-one", "email": "user-one"},
        {"id": "uuid-two", "email": "user-two"},
    ]
    live["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] = (
        "node-private-key"
    )
    live["outbounds"].insert(
        1,
        {"tag": "warp", "protocol": "wireguard", "settings": {"secretKey": "secret"}},
    )

    patched = update_script.apply_stability_profile(live, template)

    assert patched["dns"] == template["dns"]
    assert patched["policy"]["levels"]["0"] == {
        "handshake": 8,
        "connIdle": 300,
        "statsUserUplink": True,
        "statsUserDownlink": True,
        "statsUserOnline": True,
        "customPolicy": 17,
    }
    assert patched["routing"]["domainStrategy"] == "AsIs"
    assert patched["inbounds"] == live["inbounds"]
    assert patched["outbounds"] == live["outbounds"]
    assert any(
        rule.get("outboundTag") == "warp"
        for rule in patched["routing"]["rules"]
    )
    assert not any(
        rule.get("network") == "udp"
        and str(rule.get("port")) == "443"
        and rule.get("outboundTag") == "block"
        for rule in patched["routing"]["rules"]
    )
    assert not any(
        (
            "geoip:ru" in rule.get("ip", [])
            or "geosite:category-ru" in rule.get("domain", [])
        )
        and rule.get("outboundTag") == "direct"
        for rule in patched["routing"]["rules"]
    )


def test_env_update_replaces_idle_and_preserves_unrelated_values():
    current = "HOST_IP=192.0.2.10\nXRAY_CONN_IDLE=30\nCONTROL_MODE=apply\n"

    updated = update_script.update_env_values(
        current,
        {"XRAY_CONN_IDLE": "300"},
    )

    assert updated == (
        "HOST_IP=192.0.2.10\n"
        "XRAY_CONN_IDLE=300\n"
        "CONTROL_MODE=apply\n"
    )


def test_stability_rollout_validates_candidate_before_restart(monkeypatch):
    template = json.loads((ROOT / "agent/template.json").read_text())
    live = deepcopy(template)
    live["policy"]["levels"]["0"].update({"handshake": 4, "connIdle": 30})
    live["routing"]["domainStrategy"] = "IPIfNonMatch"
    live["routing"]["rules"].insert(
        -1,
        {
            "type": "field",
            "network": "udp",
            "port": "443",
            "outboundTag": "block",
        },
    )
    live["inbounds"][0]["settings"]["clients"] = [
        {"id": "uuid-one", "email": "user-one"}
    ]
    remote_text = {
        update_script.REMOTE_XRAY_CONFIG: json.dumps(live),
        update_script.REMOTE_AGENT_ENV: "XRAY_CONN_IDLE=30\nPRIVATE_KEY=secret\n",
        update_script.REMOTE_VPN_ENV: "CONTROL_MODE=apply\nXRAY_CONN_IDLE=30\n",
    }
    writes: list[tuple[str, str, int]] = []
    commands: list[tuple[str, str]] = []

    monkeypatch.setattr(
        update_script,
        "_read_remote_text",
        lambda _client, path: remote_text[path],
    )
    monkeypatch.setattr(
        update_script,
        "_write_remote_text_atomic",
        lambda _client, path, text, mode=0o600: writes.append(
            (path, text, mode)
        ),
    )

    def fake_run(_client, command, label="", timeout=120):
        commands.append((label, command))
        if label == "post-stability health":
            return '{"status":"ok","clients":1}\n'
        return ""

    monkeypatch.setattr(update_script, "run", fake_run)

    update_script.patch_node_stability(object(), "192.0.2.10")

    candidate_path, candidate_text, candidate_mode = writes[0]
    candidate = json.loads(candidate_text)
    assert candidate_path == update_script.REMOTE_XRAY_CONFIG + ".candidate.json"
    assert candidate_mode == 0o600
    assert candidate["inbounds"] == live["inbounds"]
    assert candidate["outbounds"] == live["outbounds"]
    assert candidate["policy"]["levels"]["0"]["connIdle"] == 300
    assert candidate["routing"]["domainStrategy"] == "AsIs"
    assert not any(
        rule.get("network") == "udp" and str(rule.get("port")) == "443"
        for rule in candidate["routing"]["rules"]
    )

    env_writes = {path: text for path, text, _mode in writes[1:]}
    assert "XRAY_CONN_IDLE=300\n" in env_writes[update_script.REMOTE_AGENT_ENV]
    assert "XRAY_CONN_IDLE=300\n" in env_writes[update_script.REMOTE_VPN_ENV]

    labels = [label for label, _command in commands]
    assert labels.index("validate stability candidate") < labels.index(
        "activate stability candidate"
    )
    assert labels.index("activate stability candidate") < labels.index(
        "restart xray"
    )
    assert "post-stability health" in labels
    assert all("bot" not in command for _label, command in commands)
