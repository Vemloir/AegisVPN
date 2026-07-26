"""Credential and promotion primitives for the outbound node control plane.

This module intentionally does not print secrets. Operator scripts may copy the
returned files to a node, while the central database stores only the token hash
and certificate fingerprint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True, slots=True)
class NodeCredentials:
    client_cert: Path
    client_key: Path
    ca_cert: Path
    token_file: Path
    token: str = field(repr=False)
    token_hash: str
    cert_fingerprint: str


@dataclass(frozen=True, slots=True)
class PromotionState:
    desired_generation: int
    applied_generation: int
    desired_digest: str | None
    applied_digest: str | None
    last_seen_at: datetime | None
    last_error: str | None


def render_node_control_env(
    *,
    control_urls: list[str],
    mode: str,
    bind_host: str | None = None,
) -> str:
    """Render the non-secret node settings used during rollout.

    The control channel deliberately supports only ordinary HTTPS on TCP/443.
    That keeps it usable on networks which block WireGuard, QUIC and unusual
    destination ports.
    """
    if mode not in {"observe", "apply"}:
        raise ValueError("node control mode must be observe or apply")
    normalized: list[str] = []
    for raw_url in control_urls:
        url = raw_url.strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port not in {None, 443}
        ):
            raise ValueError("control URL must use standard HTTPS on TCP/443")
        normalized.append(url)
    if not normalized:
        raise ValueError("at least one control URL is required")

    host = bind_host or ("0.0.0.0" if mode == "observe" else "127.0.0.1")
    if host not in {"0.0.0.0", "127.0.0.1"}:
        raise ValueError("agent bind host must be 0.0.0.0 or 127.0.0.1")
    return "\n".join(
        (
            f"CONTROL_MODE={mode}",
            f"CONTROL_URLS={','.join(normalized)}",
            "CONTROL_TOKEN_FILE=/data/control/token",
            "CONTROL_CLIENT_CERT=/data/control/client.crt",
            "CONTROL_CLIENT_KEY=/data/control/client.key",
            "CONTROL_CA_CERT=/data/control/ca.crt",
            f"AGENT_BIND_HOST={host}",
            "",
        )
    )


def render_agent_firewall(
    *,
    control_server_ip: str,
    public_agent: bool,
) -> str:
    """Return an idempotent iptables policy scoped only to TCP/8444.

    In pull mode the port is rejected with a TCP reset, so it is both
    unreachable and does not look like a hanging service to external scanners.
    During rollback it is reachable solely from the fixed control-server IP.
    Xray/Hysteria ports are intentionally not mentioned, so rollout cannot
    mutate the data-plane firewall.
    """
    address = ip_address(control_server_ip)
    if address.version != 4:
        raise ValueError("the rollback control-server address must be a fixed IPv4")
    chain = "AEGIS_AGENT_API"
    commands = [
        f"iptables -N {chain} 2>/dev/null || true",
        f"iptables -F {chain}",
        f"iptables -A {chain} -p tcp --dport 8444 "
        "-s 127.0.0.0/8 -j ACCEPT",
    ]
    if public_agent:
        commands.append(
            f"iptables -A {chain} -p tcp --dport 8444 "
            f"-s {address.compressed} -j ACCEPT"
        )
    commands.extend(
        (
            f"iptables -A {chain} -p tcp --dport 8444 "
            "-j REJECT --reject-with tcp-reset",
            (
                f"iptables -C INPUT -p tcp --dport 8444 -j {chain} "
                f"2>/dev/null || iptables -I INPUT 1 -p tcp --dport 8444 "
                f"-j {chain}"
            ),
        )
    )
    return "\n".join(commands) + "\n"


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, 0o600)


def initialize_control_server(
    *,
    ca_cert: Path,
    output_dir: Path,
    caddy_template: Path,
) -> None:
    """Create the non-CA-key material copied to the central control host."""
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    target_ca = output_dir / "client-ca.crt"
    shutil.copyfile(ca_cert, target_ca)
    os.chmod(target_ca, 0o644)

    proxy_secret = output_dir / "proxy-secret"
    if not proxy_secret.exists():
        _write_private(proxy_secret, secrets.token_urlsafe(48).encode())
    else:
        os.chmod(proxy_secret, 0o600)

    target_caddy = output_dir / "control.caddy"
    shutil.copyfile(caddy_template, target_caddy)
    os.chmod(target_caddy, 0o644)


def ensure_control_ca(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    cert_path = directory / "client-ca.crt"
    key_path = directory / "client-ca.key"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    if cert_path.exists() != key_path.exists():
        raise ValueError("control CA is incomplete; refusing to replace one half")

    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "AegisVPN Node Control CA")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    _write_private(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    os.chmod(cert_path, 0o644)
    return cert_path, key_path


def issue_node_credentials(
    *,
    ca_cert: Path,
    ca_key: Path,
    output_dir: Path,
    node_name: str,
) -> NodeCredentials:
    if not node_name or len(node_name) > 128:
        raise ValueError("node_name must contain 1..128 characters")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)

    issuer_cert = x509.load_pem_x509_certificate(ca_cert.read_bytes())
    issuer_key = serialization.load_pem_private_key(
        ca_key.read_bytes(),
        password=None,
    )
    client_key_object = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_name)])
        )
        .issuer_name(issuer_cert.subject)
        .public_key(client_key_object.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(min(now + timedelta(days=397), issuer_cert.not_valid_after_utc))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(issuer_key, hashes.SHA256())
    )

    client_cert = output_dir / "client.crt"
    client_key = output_dir / "client.key"
    copied_ca = output_dir / "ca.crt"
    token_file = output_dir / "token"
    client_cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    os.chmod(client_cert, 0o644)
    _write_private(
        client_key,
        client_key_object.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    shutil.copyfile(ca_cert, copied_ca)
    os.chmod(copied_ca, 0o644)
    token = secrets.token_urlsafe(48)
    _write_private(token_file, token.encode())
    return NodeCredentials(
        client_cert=client_cert,
        client_key=client_key,
        ca_cert=copied_ca,
        token_file=token_file,
        token=token,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        cert_fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
    )


def validate_promotion(
    state: PromotionState,
    *,
    now: datetime | None = None,
    max_age_seconds: int = 90,
) -> None:
    current_time = now or datetime.now(UTC).replace(tzinfo=None)
    if (
        state.desired_generation < 1
        or state.desired_generation != state.applied_generation
    ):
        raise ValueError("desired/applied generation mismatch")
    if (
        not state.desired_digest
        or state.desired_digest != state.applied_digest
    ):
        raise ValueError("desired/applied digest mismatch")
    if state.last_error:
        raise ValueError("node reports a control error")
    if state.last_seen_at is None:
        raise ValueError("node heartbeat is missing")

    last_seen = state.last_seen_at
    if current_time.tzinfo is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=current_time.tzinfo)
    elif current_time.tzinfo is None and last_seen.tzinfo is not None:
        last_seen = last_seen.replace(tzinfo=None)
    age = (current_time - last_seen).total_seconds()
    if age < 0 or age > max_age_seconds:
        raise ValueError("node heartbeat is stale")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize AegisVPN outbound-control credentials."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    central = commands.add_parser(
        "init-central",
        help="Create the CA, proxy secret and Caddy include.",
    )
    central.add_argument(
        "--ca-dir",
        required=True,
        type=Path,
        help="Operator-only directory for client-ca.crt/client-ca.key.",
    )
    central.add_argument(
        "--server-output",
        required=True,
        type=Path,
        help="Directory to upload/mount as data/control/server on the control host.",
    )
    node = commands.add_parser(
        "issue-node",
        help="Issue one node certificate, key and token.",
    )
    node.add_argument("--ca-dir", required=True, type=Path)
    node.add_argument("--node-output", required=True, type=Path)
    node.add_argument("--node-name", required=True)
    args = parser.parse_args()
    ca_cert, ca_key = ensure_control_ca(args.ca_dir.expanduser().resolve())
    if args.command == "init-central":
        initialize_control_server(
            ca_cert=ca_cert,
            output_dir=args.server_output.expanduser().resolve(),
            caddy_template=(
                Path(__file__).resolve().parent
                / "control-plane"
                / "control.caddy.example"
            ),
        )
        print("control server material initialized; no secrets were printed")
    else:
        credentials = issue_node_credentials(
            ca_cert=ca_cert,
            ca_key=ca_key,
            output_dir=args.node_output.expanduser().resolve(),
            node_name=args.node_name,
        )
        print(
            json.dumps(
                {
                    "token_hash": credentials.token_hash,
                    "cert_fingerprint": credentials.cert_fingerprint,
                    "output_dir": str(args.node_output.expanduser().resolve()),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
