#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass
class FailoverConfig:
    api_token: str
    zone_id: str
    zone_name: str
    record_names: tuple[str, ...]
    target_ipv4: str | None
    target_ipv6: str | None
    patroni_health_url: str
    application_health_url: str
    rollback_file: Path
    api_base_url: str = "https://api.cloudflare.com/client/v4"
    timeout_seconds: float = 5.0
    patroni_tls_ca: Path | None = None
    patroni_tls_cert: Path | None = None
    patroni_tls_key: Path | None = None


def _validate(config: FailoverConfig) -> None:
    if not config.api_token or not config.zone_id:
        raise ValueError("Cloudflare API token and zone id are required")
    zone = config.zone_name.rstrip(".").lower()
    if not zone:
        raise ValueError("zone name is required")
    if not config.record_names:
        raise ValueError("at least one DNS record is required")
    for raw_name in config.record_names:
        name = raw_name.rstrip(".").lower()
        if name != zone and not name.endswith(f".{zone}"):
            raise ValueError(f"record {raw_name!r} is outside zone {zone!r}")
    if not config.target_ipv4 and not config.target_ipv6:
        raise ValueError("at least one target IP is required")
    if config.target_ipv4:
        if ipaddress.ip_address(config.target_ipv4).version != 4:
            raise ValueError("target_ipv4 must be IPv4")
    if config.target_ipv6:
        if ipaddress.ip_address(config.target_ipv6).version != 6:
            raise ValueError("target_ipv6 must be IPv6")


def _request_json(
    config: FailoverConfig,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    query: dict[str, str] | None = None,
) -> dict:
    url = f"{config.api_base_url.rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {config.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            body = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Cloudflare API request failed: {type(exc).__name__}") from exc
    if not body.get("success"):
        errors = body.get("errors") or []
        messages = [str(item.get("message", "unknown")) for item in errors if isinstance(item, dict)]
        raise RuntimeError(f"Cloudflare API rejected request: {', '.join(messages) or 'unknown error'}")
    return body


def _healthy(
    url: str,
    timeout_seconds: float,
    *,
    tls_ca: Path | None = None,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
) -> bool:
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    context = None
    if url.lower().startswith("https://"):
        context = ssl.create_default_context(cafile=str(tls_ca) if tls_ca else None)
        if tls_cert and tls_key:
            context.load_cert_chain(str(tls_cert), str(tls_key))
    try:
        with urlopen(request, timeout=timeout_seconds, context=context) as response:
            return response.status == 200
    except (HTTPError, URLError, TimeoutError):
        return False


def _get_record(config: FailoverConfig, record_type: str, name: str) -> dict:
    body = _request_json(
        config,
        "GET",
        f"zones/{config.zone_id}/dns_records",
        query={"type": record_type, "name": name},
    )
    records = body.get("result") or []
    if len(records) != 1:
        raise RuntimeError(f"expected exactly one {record_type} record for {name}, got {len(records)}")
    return records[0]


def _put_record(config: FailoverConfig, record: dict, payload: dict) -> None:
    _request_json(
        config,
        "PUT",
        f"zones/{config.zone_id}/dns_records/{record['id']}",
        payload=payload,
    )


def _atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def run_failover(config: FailoverConfig, *, role: str) -> dict:
    _validate(config)
    if role.lower() not in {"primary", "master", "leader"}:
        return {"status": "skipped", "reason": "not-primary", "changed": 0}
    if not _healthy(
        config.patroni_health_url,
        config.timeout_seconds,
        tls_ca=config.patroni_tls_ca,
        tls_cert=config.patroni_tls_cert,
        tls_key=config.patroni_tls_key,
    ):
        return {"status": "skipped", "reason": "patroni-unhealthy", "changed": 0}
    if not _healthy(config.application_health_url, config.timeout_seconds):
        return {"status": "skipped", "reason": "application-unhealthy", "changed": 0}

    targets = (("A", config.target_ipv4), ("AAAA", config.target_ipv6))
    changes: list[tuple[dict, dict]] = []
    previous: list[dict] = []
    for record_type, target in targets:
        if not target:
            continue
        for raw_name in config.record_names:
            name = raw_name.rstrip(".").lower()
            record = _get_record(config, record_type, name)
            payload = {
                "type": record_type,
                "name": name,
                "content": target,
                "ttl": 60,
                "proxied": False,
            }
            if all(record.get(key) == value for key, value in payload.items()):
                continue
            previous.append(
                {
                    "id": record["id"],
                    "type": record["type"],
                    "name": record["name"],
                    "content": record["content"],
                    "ttl": record["ttl"],
                    "proxied": bool(record.get("proxied", False)),
                }
            )
            changes.append((record, payload))

    if not changes:
        return {"status": "unchanged", "changed": 0}

    _atomic_private_json(
        config.rollback_file,
        {
            "zone_id": config.zone_id,
            "zone_name": config.zone_name,
            "records": previous,
        },
    )
    for record, payload in changes:
        _put_record(config, record, payload)
    return {"status": "updated", "changed": len(changes)}


def restore_snapshot(config: FailoverConfig, snapshot_file: Path) -> dict:
    _validate(config)
    snapshot = json.loads(snapshot_file.read_text())
    if snapshot.get("zone_id") != config.zone_id or snapshot.get("zone_name") != config.zone_name:
        raise ValueError("rollback snapshot belongs to a different Cloudflare zone")
    allowed = {name.rstrip(".").lower() for name in config.record_names}
    records = snapshot.get("records") or []
    changed = 0
    for record in records:
        if record.get("name", "").rstrip(".").lower() not in allowed:
            raise ValueError("rollback snapshot contains a non-allowlisted record")
        current = _get_record(config, record["type"], record["name"])
        payload = {
            "type": record["type"],
            "name": record["name"],
            "content": record["content"],
            "ttl": int(record["ttl"]),
            "proxied": bool(record["proxied"]),
        }
        if all(current.get(key) == value for key, value in payload.items()):
            continue
        _put_record(config, current, payload)
        changed += 1
    return {"status": "restored" if changed else "unchanged", "changed": changed}


def config_from_env() -> FailoverConfig:
    record_names = tuple(
        value.strip()
        for value in os.environ.get("CLOUDFLARE_FAILOVER_RECORDS", "").split(",")
        if value.strip()
    )
    return FailoverConfig(
        api_token=os.environ.get("CLOUDFLARE_API_TOKEN", ""),
        zone_id=os.environ.get("CLOUDFLARE_ZONE_ID", ""),
        zone_name=os.environ.get("CLOUDFLARE_ZONE_NAME", "aegisvpn.org"),
        record_names=record_names,
        target_ipv4=os.environ.get("FAILOVER_TARGET_IPV4") or None,
        target_ipv6=os.environ.get("FAILOVER_TARGET_IPV6") or None,
        patroni_health_url=os.environ.get(
            "PATRONI_PRIMARY_HEALTH_URL",
            "https://127.0.0.1:8008/primary",
        ),
        application_health_url=os.environ.get(
            "APPLICATION_HEALTH_URL",
            "http://127.0.0.1:8080/health",
        ),
        rollback_file=Path(
            os.environ.get(
                "CLOUDFLARE_ROLLBACK_FILE",
                "/var/lib/aegis-ha/cloudflare-rollback.json",
            )
        ),
        patroni_tls_ca=Path(os.environ["PATRONI_TLS_CA"]) if os.environ.get("PATRONI_TLS_CA") else None,
        patroni_tls_cert=Path(os.environ["PATRONI_TLS_CERT"]) if os.environ.get("PATRONI_TLS_CERT") else None,
        patroni_tls_key=Path(os.environ["PATRONI_TLS_KEY"]) if os.environ.get("PATRONI_TLS_KEY") else None,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", default=os.environ.get("PATRONI_ROLE", ""))
    parser.add_argument("--restore", type=Path)
    args = parser.parse_args()
    config = config_from_env()
    report = restore_snapshot(config, args.restore) if args.restore else run_failover(config, role=args.role)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
