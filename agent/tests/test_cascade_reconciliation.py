import pytest

from app.cascade import CascadeConfigError, apply_cascade_routes
from app.control_models import DesiredCascadeRoute


def route(route_id: int = 4, inbound_tags: list[str] | None = None) -> DesiredCascadeRoute:
    return DesiredCascadeRoute(
        kind="cascade_route",
        route_id=route_id,
        revision=2,
        config_digest="a" * 64,
        label="Russia → Germany | Frankfurt",
        inbound_tags=inbound_tags or ["vless-in"],
        exits=[
            {
                "position": 0,
                "host": "198.51.100.11",
                "port": 443,
                "uuid": "10000000-0000-0000-0000-000000000001",
                "public_key": "pk-one",
                "short_id": "sid-one",
                "server_name": "www.cloudflare.com",
                "xhttp_path": "/cascade-a",
            },
            {
                "position": 1,
                "host": "198.51.100.12",
                "port": 443,
                "uuid": "10000000-0000-0000-0000-000000000002",
                "public_key": "pk-two",
                "short_id": "sid-two",
                "server_name": "www.microsoft.com",
                "xhttp_path": "/cascade-b",
            },
        ],
        health_policy={"strategy": "leastPing", "probe_interval": "10s"},
    )


def base_config() -> dict:
    return {
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "network": "tcp,udp", "outboundTag": "direct"},
            ]
        },
    }


def test_entry_builds_reality_xhttp_balancer_without_direct_fallback():
    config = base_config()
    assert apply_cascade_routes(config, [route()]) is True

    managed = [outbound for outbound in config["outbounds"] if outbound["tag"].startswith("cascade-")]
    assert len(managed) == 2
    assert all(outbound["protocol"] == "vless" for outbound in managed)
    assert all(outbound["streamSettings"]["network"] == "xhttp" for outbound in managed)
    assert all(outbound["streamSettings"]["security"] == "reality" for outbound in managed)
    cascade_rule = next(rule for rule in config["routing"]["rules"] if "balancerTag" in rule)
    assert cascade_rule["inboundTag"] == ["vless-in"]
    assert "outboundTag" not in cascade_rule
    assert config["routing"]["balancers"][0]["selector"] == [
        "cascade-4-exit-0",
        "cascade-4-exit-1",
    ]
    assert config["observatory"]["subjectSelector"] == [
        "cascade-4-exit-0",
        "cascade-4-exit-1",
    ]


def test_revocation_removes_managed_routes_and_never_leaves_direct_route_for_entry():
    config = base_config()
    apply_cascade_routes(config, [route()])
    assert apply_cascade_routes(config, []) is True
    assert not any(outbound["tag"].startswith("cascade-") for outbound in config["outbounds"])
    assert not any("balancerTag" in rule for rule in config["routing"]["rules"])
    assert "observatory" not in config


def test_two_routes_cannot_capture_the_same_client_inbound():
    with pytest.raises(CascadeConfigError, match="inbound"):
        apply_cascade_routes(base_config(), [route(4), route(5)])
