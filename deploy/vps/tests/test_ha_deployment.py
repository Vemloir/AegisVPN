from pathlib import Path

import pytest

from deploy.vps.ha.migrate_sqlite_to_postgres import canonical_rows_digest
from deploy.vps.ha.verify_failover import validate_cluster_state


ROOT = Path(__file__).resolve().parents[3]
HA = ROOT / "deploy/vps/ha"


def test_ha_topology_has_two_postgres_members_three_etcd_votes_and_local_haproxy():
    compose = (HA / "docker-compose.ha.yml").read_text()
    patroni = (HA / "patroni.yml").read_text()
    example = (HA / "etcd.env.example").read_text()
    haproxy = (HA / "haproxy.cfg").read_text()
    database = (ROOT / "bot/src/core/database.py").read_text()

    assert "latest" not in compose.lower()
    assert "PATRONI_NAME" in compose
    assert "ETCD_INITIAL_CLUSTER" in compose
    assert "ETCD_CLIENT_CERT_AUTH=true" in compose
    assert "ETCD_PEER_CLIENT_CERT_AUTH=true" in compose
    assert "PL_HA_IP" in example
    assert "USA_HA_IP" in example
    assert "GERMANY_ETCD_IP" in example
    assert "synchronous_mode: true" in patroni
    assert "maximum_lag_on_failover" in patroni
    assert "bind 127.0.0.1:5433" in haproxy
    assert "option httpchk GET /primary" in haproxy
    assert "pool_pre_ping=True" in database


def test_failover_verifier_requires_exactly_one_writable_node():
    validate_cluster_state(
        [
            {"name": "pl", "writable": True, "timeline": 4},
            {"name": "us", "writable": False, "timeline": 4},
        ]
    )
    with pytest.raises(ValueError, match="exactly one"):
        validate_cluster_state(
            [
                {"name": "pl", "writable": True, "timeline": 4},
                {"name": "us", "writable": True, "timeline": 4},
            ]
        )
    with pytest.raises(ValueError, match="exactly one"):
        validate_cluster_state(
            [
                {"name": "pl", "writable": False, "timeline": 4},
                {"name": "us", "writable": False, "timeline": 4},
            ]
        )


def test_migration_digest_is_order_independent_but_value_sensitive():
    first = canonical_rows_digest([{"id": 2, "v": b"x"}, {"id": 1, "v": None}])
    second = canonical_rows_digest([{"v": None, "id": 1}, {"v": b"x", "id": 2}])
    changed = canonical_rows_digest([{"id": 2, "v": b"y"}, {"id": 1, "v": None}])

    assert first == second
    assert first != changed
