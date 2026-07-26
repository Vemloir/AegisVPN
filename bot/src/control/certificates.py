from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


@dataclass(frozen=True, slots=True)
class Hy2CertificateBundle:
    certificate: str
    private_key: str
    hostname: str
    fingerprint: str
    not_after: datetime


def _read_bounded(path: Path, maximum: int = 1_048_576) -> bytes:
    data = path.read_bytes()
    if not data or len(data) > maximum:
        raise ValueError(f"invalid certificate bundle file: {path.name}")
    return data


def _certificate_names(certificate: x509.Certificate) -> set[str]:
    try:
        extension = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
    except x509.ExtensionNotFound:
        return set()
    return set(extension.value.get_values_for_type(x509.DNSName))


def load_hy2_certificate_bundle(directory: str | Path) -> Hy2CertificateBundle:
    root = Path(directory)
    certificate_pem = _read_bounded(root / "cert.pem")
    private_key_pem = _read_bounded(root / "key.pem")
    hostname = _read_bounded(root / "hostname", maximum=255).decode().strip().lower()
    if not hostname or any(character.isspace() for character in hostname):
        raise ValueError("invalid Hy2 certificate hostname")

    certificate = x509.load_pem_x509_certificate(certificate_pem)
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    cert_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_public != key_public:
        raise ValueError("Hy2 certificate and private key do not match")
    if hostname not in _certificate_names(certificate):
        raise ValueError("Hy2 certificate does not cover exported hostname")

    return Hy2CertificateBundle(
        certificate=certificate_pem.decode(),
        private_key=private_key_pem.decode(),
        hostname=hostname,
        fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
        not_after=certificate.not_valid_after_utc,
    )
