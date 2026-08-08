from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_control_host_does_not_start_a_tcp_443_data_plane_by_default():
    compose = yaml.safe_load((ROOT / "deploy/vps/docker-compose.yml").read_text())
    services = compose["services"]

    assert services["xray"]["profiles"] == ["local-exit"]
    assert services["agent"]["profiles"] == ["local-exit"]
    assert "profiles" not in services["caddy"]
    assert services["agent"]["depends_on"] == ["xray"]
    vpn_env = (ROOT / "deploy/vps/vpn.env.example").read_text()
    assert "XRAY_PORT=9443" in vpn_env
    assert "XRAY_PORT=443\n" not in vpn_env


def test_control_host_documentation_keeps_caddy_as_the_only_tcp_443_listener():
    deploy_readme = (ROOT / "deploy/vps/README.md").read_text()
    architecture = (ROOT / "ARCHITECTURE.md").read_text()

    assert "Caddy exposes the website and subscriptions on TCP/443" in deploy_readme
    assert "local Xray port is configured in `vpn.env` and must not be TCP/443" in deploy_readme
    assert "Caddy is the only TCP/443 listener" in architecture
