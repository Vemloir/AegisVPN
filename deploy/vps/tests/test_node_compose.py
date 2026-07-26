from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_node_compose_contains_only_data_plane_services():
    compose = yaml.safe_load(
        (ROOT / "deploy/vps/docker-compose.node.yml").read_text()
    )

    assert set(compose["services"]) == {"xray", "agent", "hysteria"}
    assert compose["services"]["agent"]["pid"] == "service:xray"
    assert compose["services"]["agent"]["depends_on"] == ["xray"]
    assert compose["services"]["hysteria"]["profiles"] == ["hysteria"]


def test_node_compose_never_requires_control_plane_env_files():
    text = (ROOT / "deploy/vps/docker-compose.node.yml").read_text()

    assert "bot.env" not in text
    assert "support.env" not in text
    assert "vpn.env" in text
