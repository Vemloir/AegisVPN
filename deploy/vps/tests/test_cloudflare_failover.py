from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from deploy.vps.ha.cloudflare_failover import FailoverConfig, restore_snapshot, run_failover


class CloudflareFake:
    def __init__(self) -> None:
        self.records = [
            {"id": "1", "type": "A", "name": "aegisvpn.org", "content": "192.0.2.1", "ttl": 300, "proxied": True},
            {"id": "2", "type": "A", "name": "www.aegisvpn.org", "content": "192.0.2.1", "ttl": 300, "proxied": True},
            {"id": "3", "type": "A", "name": "sub.aegisvpn.org", "content": "192.0.2.1", "ttl": 300, "proxied": True},
            {"id": "4", "type": "A", "name": "control.aegisvpn.org", "content": "192.0.2.1", "ttl": 300, "proxied": True},
            {"id": "5", "type": "A", "name": "unrelated.aegisvpn.org", "content": "192.0.2.99", "ttl": 120, "proxied": False},
        ]
        self.updates: list[dict] = []
        self.patroni_healthy = True
        self.application_healthy = True
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/patroni/primary":
                    self._json(200 if outer.patroni_healthy else 503, {"role": "primary"})
                    return
                if parsed.path == "/app/health":
                    self._json(200 if outer.application_healthy else 503, {"status": "ok"})
                    return
                if parsed.path == "/client/v4/zones/zone/dns_records":
                    assert self.headers["Authorization"] == "Bearer scoped-secret"
                    query = parse_qs(parsed.query)
                    result = [
                        record
                        for record in outer.records
                        if record["type"] == query["type"][0] and record["name"] == query["name"][0]
                    ]
                    self._json(200, {"success": True, "errors": [], "messages": [], "result": result})
                    return
                self._json(404, {"success": False, "errors": [{"message": "not found"}], "result": None})

            def do_PUT(self):  # noqa: N802
                record_id = self.path.rsplit("/", 1)[-1]
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                record = next(record for record in outer.records if record["id"] == record_id)
                record.update(payload)
                outer.updates.append(payload)
                self._json(200, {"success": True, "errors": [], "messages": [], "result": record})

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


def config(fake: CloudflareFake, rollback: Path) -> FailoverConfig:
    return FailoverConfig(
        api_token="scoped-secret",
        zone_id="zone",
        zone_name="aegisvpn.org",
        record_names=(
            "aegisvpn.org",
            "www.aegisvpn.org",
            "sub.aegisvpn.org",
            "control.aegisvpn.org",
        ),
        target_ipv4="198.51.100.10",
        target_ipv6=None,
        patroni_health_url=f"{fake.base_url}/patroni/primary",
        application_health_url=f"{fake.base_url}/app/health",
        api_base_url=f"{fake.base_url}/client/v4",
        rollback_file=rollback,
    )


def test_replica_or_unhealthy_application_never_changes_dns(tmp_path):
    with CloudflareFake() as fake:
        cfg = config(fake, tmp_path / "rollback.json")
        assert run_failover(cfg, role="replica")["status"] == "skipped"
        fake.application_healthy = False
        assert run_failover(cfg, role="primary")["status"] == "skipped"
        assert fake.updates == []


def test_primary_updates_only_allowlisted_dns_only_records_and_is_idempotent(tmp_path):
    with CloudflareFake() as fake:
        cfg = config(fake, tmp_path / "rollback.json")
        report = run_failover(cfg, role="primary")

        assert report == {"status": "updated", "changed": 4}
        assert len(fake.updates) == 4
        assert all(update["content"] == "198.51.100.10" for update in fake.updates)
        assert all(update["ttl"] == 60 for update in fake.updates)
        assert all(update["proxied"] is False for update in fake.updates)
        assert next(record for record in fake.records if record["id"] == "5")["content"] == "192.0.2.99"

        assert run_failover(cfg, role="primary") == {"status": "unchanged", "changed": 0}
        assert len(fake.updates) == 4


def test_rollback_snapshot_has_previous_values_but_never_token(tmp_path):
    rollback = tmp_path / "rollback.json"
    with CloudflareFake() as fake:
        cfg = config(fake, rollback)
        run_failover(cfg, role="primary")

    payload = rollback.read_text()
    assert "scoped-secret" not in payload
    assert "192.0.2.1" in payload
    assert (rollback.stat().st_mode & 0o777) == 0o600

    with CloudflareFake() as fake:
        cfg = config(fake, rollback)
        for record in fake.records[:4]:
            record.update({"content": "198.51.100.10", "ttl": 60, "proxied": False})
        assert restore_snapshot(cfg, rollback) == {"status": "restored", "changed": 4}
        assert all(record["content"] == "192.0.2.1" for record in fake.records[:4])


def test_rejects_records_outside_configured_zone(tmp_path):
    with CloudflareFake() as fake:
        cfg = config(fake, tmp_path / "rollback.json")
        cfg = FailoverConfig(**{**cfg.__dict__, "record_names": ("evil.example",)})
        with pytest.raises(ValueError, match="outside zone"):
            run_failover(cfg, role="primary")
