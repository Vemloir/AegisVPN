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
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return set()
    return set(extension.value.get_values_for_type(x509.DNSName))


def _name_covers(pattern: str, hostname: str) -> bool:
    pattern = pattern.lower()
    if pattern == hostname:
        return True
    # One-label wildcard only, as TLS clients match it.
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return hostname.endswith(suffix) and hostname.count(".") == pattern.count(".")
    return False


def load_cdn_certificate_bundle(directory: str | Path, hostname: str) -> Hy2CertificateBundle:
    """The CDN origin pair for ``hostname`` (the node's ``cdn_origin_host``).

    Unlike Hy2, a wildcard for the origin domain may serve every node: the
    hostname comes from the node row, and the certificate must merely cover it.
    """
    root = Path(directory)
    hostname = hostname.strip().lower()
    if not hostname or any(character.isspace() for character in hostname):
        raise ValueError("invalid CDN origin hostname")
    certificate_pem = _read_bounded(root / "cert.pem")
    private_key_pem = _read_bounded(root / "key.pem")
    certificate = _load_matching_pair(certificate_pem, private_key_pem)
    if not any(_name_covers(name, hostname) for name in _certificate_names(certificate)):
        raise ValueError("CDN certificate does not cover the origin hostname")
    return Hy2CertificateBundle(
        certificate=certificate_pem.decode(),
        private_key=private_key_pem.decode(),
        hostname=hostname,
        fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
        not_after=certificate.not_valid_after_utc,
    )


def _load_matching_pair(certificate_pem: bytes, private_key_pem: bytes) -> x509.Certificate:
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
        raise ValueError("certificate and private key do not match")
    return certificate


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
