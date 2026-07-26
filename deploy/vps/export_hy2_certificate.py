#!/usr/bin/env python3
"""Export Caddy's renewed Hy2 certificate for authenticated node delivery."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


DEFAULT_CADDY_CERTIFICATES = Path(
    "/root/aegis/deploy/vps/data/caddy/caddy/certificates"
)
DEFAULT_OUTPUT = Path(
    "/root/aegis/deploy/vps/data/control/server/hy2-cert"
)


def _run(*arguments: str) -> bytes:
    return subprocess.run(
        arguments,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _discover_hostname(certificates: Path) -> str:
    candidates = sorted(
        {
            path.parent.name
            for path in certificates.rglob("*.crt")
            if path.parent.name in path.name
        }
    )
    duckdns = [hostname for hostname in candidates if hostname.endswith(".duckdns.org")]
    selected = duckdns or candidates
    if len(selected) != 1:
        raise RuntimeError(
            "set HY2_CERT_DOMAIN: unable to select one Caddy certificate"
        )
    return selected[0]


def _find_pair(certificates: Path, hostname: str) -> tuple[Path, Path]:
    matches = sorted(certificates.rglob(f"{hostname}.crt"))
    if len(matches) != 1:
        raise RuntimeError(f"expected one Caddy certificate for {hostname}")
    certificate = matches[0]
    private_key = certificate.with_suffix(".key")
    if not private_key.is_file():
        raise RuntimeError(f"private key is missing for {hostname}")
    return certificate, private_key


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def export_certificate(
    *,
    certificates: Path,
    output: Path,
    hostname: str | None,
) -> str:
    selected_hostname = (hostname or _discover_hostname(certificates)).strip().lower()
    certificate, private_key = _find_pair(certificates, selected_hostname)

    _run(
        "openssl",
        "x509",
        "-in",
        str(certificate),
        "-noout",
        "-checkhost",
        selected_hostname,
    )
    _run("openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "604800")
    cert_public = _run("openssl", "x509", "-in", str(certificate), "-pubkey", "-noout")
    key_public = _run("openssl", "pkey", "-in", str(private_key), "-pubout")
    if cert_public != key_public:
        raise RuntimeError("Caddy certificate and key do not match")

    _atomic_write(output / "cert.pem", certificate.read_bytes(), 0o600)
    _atomic_write(output / "key.pem", private_key.read_bytes(), 0o600)
    _atomic_write(output / "hostname", (selected_hostname + "\n").encode(), 0o600)
    return selected_hostname


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--certificates", type=Path, default=DEFAULT_CADDY_CERTIFICATES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--hostname", default=os.environ.get("HY2_CERT_DOMAIN"))
    arguments = parser.parse_args()
    hostname = export_certificate(
        certificates=arguments.certificates,
        output=arguments.output,
        hostname=arguments.hostname,
    )
    print(f"Exported renewed Hy2 certificate for {hostname}.")


if __name__ == "__main__":
    main()
