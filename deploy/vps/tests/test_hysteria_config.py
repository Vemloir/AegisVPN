from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_hysteria_disables_path_mtu_discovery_for_filtered_paths():
    config = (ROOT / "deploy/vps/hysteria/config.template.yaml").read_text()

    assert "disablePathMTUDiscovery: true" in config


def test_hysteria_allows_split_camouflage_and_certificate_sni():
    config = (ROOT / "deploy/vps/hysteria/config.template.yaml").read_text()

    assert "sniGuard: disable" in config
