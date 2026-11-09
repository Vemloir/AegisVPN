import base64
import tomllib
from types import SimpleNamespace

import pytest
import yaml

from deploy.vps import add_server


SECRET = base64.urlsafe_b64encode(b"\xee" + bytes(range(16)) + b"google.com").decode().rstrip("=")


def test_provisioner_uses_the_node_only_compose():
    assert add_server.COMPOSE_FILE.name == "docker-compose.node.yml"
    compose = yaml.safe_load(add_server.COMPOSE_FILE.read_text())
    runtime = add_server.parse_env(
        (add_server.ROOT / "deploy/vps/runtime-versions.env").read_text()
    )
    assert compose["services"]["mtg"]["image"] == runtime["MTG_IMAGE"]


def test_standard_node_profile_routes_gemini_through_warp():
    args = SimpleNamespace(
        xhttp_port="443", tcp_port="2053", xray_network="xhttp",
        reality_dest="example.com:443", reality_server_name="example.com",
        server_domain="192.0.2.10", control_url=["https://control.example.com"],
    )
    script = add_server.build_setup_script(args)
    assert "\nGEMINI_EGRESS=warp\n" in script
    assert "\nWARP_DOMAIN_STRATEGY=ForceIPv6\n" in script
    assert "GEMINI_EGRESS=ipv4" not in script


def test_mtproxy_config_accepts_base64_and_hex_without_exposing_identity():
    for secret in (SECRET, (b"\xee" + bytes(range(16)) + b"google.com").hex()):
        config = tomllib.loads(add_server.render_mtproxy_config(secret))
        assert config == {"secret": secret, "bind-to": "0.0.0.0:80"}
    for secret in ("", "not-a-fake-tls-secret", "hello\nworld"):
        with pytest.raises(ValueError, match="Invalid fake-TLS"):
            add_server.render_mtproxy_config(secret)
    with pytest.raises(ValueError):
        add_server.render_mtproxy_config(SECRET, port=0)


def test_mtproxy_reuses_identity_and_runs_compose_without_secret_arguments(monkeypatch, capsys):
    commands = []
    written = []

    def run(client, command, label, timeout=120):
        commands.append(command)
        if label == "read existing MTProxy identity":
            return SECRET + "\n"
        if label == "verify MTProxy":
            return "true\n"
        return ""

    monkeypatch.setattr(add_server, "run_or_die", run)
    monkeypatch.setattr(add_server, "write_remote_script", lambda *args: written.append(args))
    assert add_server.configure_mtproxy(object()) == SECRET
    assert not any("generate-secret" in command for command in commands)
    assert all(SECRET not in command for command in commands)
    assert any("docker compose --profile mtproxy up -d --no-deps mtg" in c for c in commands)
    assert any("chmod 0600" in command for command in commands)
    assert tomllib.loads(written[0][2])["secret"] == SECRET
    assert SECRET not in capsys.readouterr().out


def test_mtproxy_generation_and_startup_are_fail_closed(monkeypatch):
    def run(client, command, label, timeout=120):
        if label == "read existing MTProxy identity":
            return ""
        if label == "generate MTProxy identity":
            assert "@sha256:" in command
            return SECRET
        if label == "verify MTProxy":
            return "false\n"
        return ""

    monkeypatch.setattr(add_server, "run_or_die", run)
    monkeypatch.setattr(add_server, "write_remote_script", lambda *args: None)
    with pytest.raises(SystemExit, match="database advertisement was not updated"):
        add_server.configure_mtproxy(object())


def test_warp_missing_credentials_stop_before_restarting_any_service(monkeypatch):
    commands = []

    def run(client, command, label, timeout=120):
        commands.append(command)
        return "WARP_SECRET_KEY=only-one-field\n" if label == "validate WARP registration" else ""

    monkeypatch.setattr(add_server, "run_or_die", run)
    monkeypatch.setattr(add_server, "write_remote_script", lambda *args: None)
    with pytest.raises(SystemExit, match="WARP registration incomplete"):
        add_server.configure_warp(object())
    assert not any("docker compose" in command for command in commands)


def test_warp_is_actually_applied_after_registration(monkeypatch):
    commands = []
    fields = ("WARP_SECRET_KEY", "WARP_ADDR_V4", "WARP_ADDR_V6", "WARP_PEER_PUBKEY", "WARP_ENDPOINT", "WARP_RESERVED")

    def run(client, command, label, timeout=120):
        commands.append(command)
        return "\n".join(f"{key}=test-value" for key in fields) if label == "validate WARP registration" else ""

    monkeypatch.setattr(add_server, "run_or_die", run)
    monkeypatch.setattr(add_server, "write_remote_script", lambda *args: None)
    add_server.configure_warp(object())
    apply = commands[-1]
    assert "xray run -test" in apply
    assert "docker compose restart xray" in apply
    assert apply.count("--force-recreate agent") == 2
    assert "| tail" not in apply
    assert "rm -f" not in apply
