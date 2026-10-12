from __future__ import annotations

import asyncio
import inspect
import os
import random
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from .config import settings
from .control_client import ControlClient


@dataclass(frozen=True, slots=True)
class CertificateSyncResult:
    status: str
    fingerprint: str | None = None


def _dns_matches(pattern: str, hostname: str) -> bool:
    pattern = pattern.lower()
    hostname = hostname.lower()
    if pattern == hostname:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return hostname.endswith(suffix) and hostname.count(".") == pattern.count(".")
    return False


def _validate_bundle(
    certificate_pem: bytes,
    private_key_pem: bytes,
    hostname: str,
    minimum_validity: timedelta,
) -> tuple[x509.Certificate, str]:
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
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.DNSName
        )
    except x509.ExtensionNotFound:
        names = []
    if not any(_dns_matches(name, hostname) for name in names):
        raise ValueError("certificate does not cover hostname")
    if certificate.not_valid_after_utc <= datetime.now(UTC) + minimum_validity:
        raise ValueError("certificate expires too soon")
    return certificate, certificate.fingerprint(hashes.SHA256()).hex()


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _default_restart_notifier() -> None:
    marker = Path(settings.hy2_certificate_reload_marker)
    _atomic_write(marker, b"reload\n")


def _install_pair(
    certificate_path: Path,
    private_key_path: Path,
    certificate_pem: bytes,
    private_key_pem: bytes,
) -> None:
    if certificate_path.name != "cert.pem" or private_key_path.name != "key.pem":
        raise ValueError("certificate paths must end in cert.pem and key.pem")
    if certificate_path.parent != private_key_path.parent:
        raise ValueError("certificate and private key must share one live directory")

    live_directory = certificate_path.parent
    root = live_directory.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    if live_directory.exists() and not live_directory.is_symlink():
        raise ValueError("live certificate directory must be an atomic symlink")

    versions = root / "versions"
    versions.mkdir(mode=0o700, exist_ok=True)
    version_directory = Path(tempfile.mkdtemp(prefix="version-", dir=versions))
    os.chmod(version_directory, 0o700)
    _atomic_write(version_directory / "cert.pem", certificate_pem)
    _atomic_write(version_directory / "key.pem", private_key_pem)

    temporary_link = root / f".current-{secrets.token_hex(8)}"
    try:
        os.symlink(
            os.path.relpath(version_directory, root),
            temporary_link,
            target_is_directory=True,
        )
        directory = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(directory)
            os.replace(temporary_link, live_directory)
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary_link.unlink(missing_ok=True)


def _certificate_poll_delay(seconds: int, *, random_value: float | None = None) -> float:
    value = random.random() if random_value is None else random_value
    return max(1.0, seconds * (9.0 + 2.0 * value) / 10.0)


def install_placeholder_certificate(
    certificate_path: str | Path,
    private_key_path: str | Path,
    hostname: str = "localhost",
) -> bool:
    """Install a self-signed pair when no certificate is live yet.

    Xray refuses to start a TLS inbound whose certificate file is missing, and
    the real certificate only arrives once the control loop is up. A short-lived
    placeholder keeps the inbound (and the whole process) bootable until the
    synchronizer swaps the real pair in. Returns True if one was installed.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    certificate_path = Path(certificate_path)
    private_key_path = Path(private_key_path)
    if certificate_path.exists() and private_key_path.exists():
        return False
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    _install_pair(
        certificate_path,
        private_key_path,
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return True


class CertificateSynchronizer:
    def __init__(
        self,
        client: ControlClient,
        *,
        certificate_path: str | Path,
        private_key_path: str | Path,
        restart_notifier: Callable[[], object] = _default_restart_notifier,
        minimum_validity: timedelta = timedelta(days=7),
        fetch: Callable[[], Awaitable[dict[str, str] | None]] | None = None,
    ):
        self.client = client
        self.certificate_path = Path(certificate_path)
        self.private_key_path = Path(private_key_path)
        self.restart_notifier = restart_notifier
        self.minimum_validity = minimum_validity
        # Which bundle this synchronizer tracks: the Hy2 one by default, the
        # CDN origin one when the caller passes the client's CDN fetcher.
        self.fetch = fetch or client.get_hy2_certificate

    async def check_once(self) -> CertificateSyncResult:
        bundle = await self.fetch()
        if bundle is None:
            return CertificateSyncResult("disabled")
        certificate_pem = bundle["certificate"].encode()
        private_key_pem = bundle["private_key"].encode()
        hostname = bundle["hostname"].strip().lower()
        _, fingerprint = _validate_bundle(
            certificate_pem,
            private_key_pem,
            hostname,
            self.minimum_validity,
        )
        if bundle["fingerprint"] != fingerprint:
            raise ValueError("certificate fingerprint mismatch")

        if (
            self.certificate_path.exists()
            and self.private_key_path.exists()
            and self.certificate_path.read_bytes() == certificate_pem
            and self.private_key_path.read_bytes() == private_key_pem
        ):
            return CertificateSyncResult("unchanged", fingerprint)

        # Both payloads are installed in a versioned directory. One symlink
        # rename makes the complete matching pair live atomically.
        _install_pair(
            self.certificate_path,
            self.private_key_path,
            certificate_pem,
            private_key_pem,
        )
        notified = self.restart_notifier()
        if inspect.isawaitable(notified):
            await notified
        return CertificateSyncResult("updated", fingerprint)


async def certificate_sync_loop(
    stop_event: asyncio.Event | None = None,
    *,
    sleep=asyncio.sleep,
) -> None:
    if not settings.hy2_enabled or settings.control_mode == "off":
        return
    stop = stop_event or asyncio.Event()
    client = ControlClient.from_settings()
    synchronizer = CertificateSynchronizer(
        client,
        certificate_path=settings.hy2_certificate_path,
        private_key_path=settings.hy2_private_key_path,
    )
    try:
        while not stop.is_set():
            try:
                result = await synchronizer.check_once()
                if result.status == "updated":
                    print("Hy2 certificate updated; reload requested")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Hy2 certificate sync error: {type(exc).__name__}")
            await sleep(_certificate_poll_delay(settings.hy2_certificate_check_seconds))
    finally:
        await client.close()


def _reload_xray_for_certificate() -> None:
    # Xray reads certificate files when the TLS inbound starts; a respawn is
    # what makes the new pair live. Rare (a renewal every couple of months),
    # so the dropped sessions are an acceptable price.
    from .xray import reload_xray

    reload_xray()


async def cdn_certificate_sync_loop(
    stop_event: asyncio.Event | None = None,
    *,
    sleep=asyncio.sleep,
) -> None:
    if not settings.xray_cdn_port or settings.control_mode == "off":
        return
    stop = stop_event or asyncio.Event()
    client = ControlClient.from_settings()
    synchronizer = CertificateSynchronizer(
        client,
        certificate_path=settings.cdn_certificate_path,
        private_key_path=settings.cdn_private_key_path,
        restart_notifier=_reload_xray_for_certificate,
        fetch=client.get_cdn_certificate,
    )
    try:
        while not stop.is_set():
            try:
                result = await synchronizer.check_once()
                if result.status == "updated":
                    print("CDN origin certificate updated; xray reload requested")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"CDN certificate sync error: {type(exc).__name__}")
            await sleep(_certificate_poll_delay(settings.cdn_certificate_check_seconds))
    finally:
        await client.close()
