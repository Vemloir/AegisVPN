import os
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import certificate_sync as sync_module
from app.certificate_sync import CertificateSynchronizer

HOSTNAME = "hy2.example.test"


def _pair(*, hostname: str = HOSTNAME, days: int = 30) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


class FakeClient:
    def __init__(self, bundle):
        self.bundle = bundle

    async def get_hy2_certificate(self):
        return self.bundle


def _bundle(cert: str, key: str) -> dict:
    parsed = x509.load_pem_x509_certificate(cert.encode())
    return {
        "certificate": cert,
        "private_key": key,
        "hostname": HOSTNAME,
        "fingerprint": parsed.fingerprint(hashes.SHA256()).hex(),
    }


async def test_new_certificate_is_installed_atomically_and_notified_once(tmp_path):
    cert, key = _pair()
    notifications = []
    sync = CertificateSynchronizer(
        FakeClient(_bundle(cert, key)),
        certificate_path=tmp_path / "current/cert.pem",
        private_key_path=tmp_path / "current/key.pem",
        restart_notifier=lambda: notifications.append("restart"),
    )

    result = await sync.check_once()

    assert result.status == "updated"
    assert (tmp_path / "current").is_symlink()
    assert (tmp_path / "current/cert.pem").read_text() == cert
    assert (tmp_path / "current/key.pem").read_text() == key
    assert os.stat(tmp_path / "current/cert.pem").st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "current/key.pem").st_mode & 0o777 == 0o600
    assert notifications == ["restart"]

    assert (await sync.check_once()).status == "unchanged"
    assert notifications == ["restart"]


async def test_invalid_or_expiring_bundle_never_replaces_working_files(tmp_path):
    old_cert, old_key = _pair()
    live = tmp_path / "current"
    await CertificateSynchronizer(
        FakeClient(_bundle(old_cert, old_key)),
        certificate_path=live / "cert.pem",
        private_key_path=live / "key.pem",
        restart_notifier=lambda: None,
    ).check_once()
    new_cert, _ = _pair(days=1)
    _, wrong_key = _pair()

    for bundle in (_bundle(new_cert, old_key), _bundle(old_cert, wrong_key)):
        sync = CertificateSynchronizer(
            FakeClient(bundle),
            certificate_path=live / "cert.pem",
            private_key_path=live / "key.pem",
            restart_notifier=lambda: pytest.fail("must not restart"),
            minimum_validity=timedelta(days=7),
        )
        with pytest.raises(ValueError):
            await sync.check_once()
        assert (live / "cert.pem").read_text() == old_cert
        assert (live / "key.pem").read_text() == old_key


async def test_failed_live_symlink_swap_keeps_the_old_complete_pair(monkeypatch, tmp_path):
    old_cert, old_key = _pair()
    new_cert, new_key = _pair()
    live = tmp_path / "current"
    await CertificateSynchronizer(
        FakeClient(_bundle(old_cert, old_key)),
        certificate_path=live / "cert.pem",
        private_key_path=live / "key.pem",
        restart_notifier=lambda: None,
    ).check_once()
    sync = CertificateSynchronizer(
        FakeClient(_bundle(new_cert, new_key)),
        certificate_path=live / "cert.pem",
        private_key_path=live / "key.pem",
        restart_notifier=lambda: pytest.fail("must not restart"),
    )
    replace = sync_module.os.replace

    def fail_live_swap(source, destination):
        if destination == live:
            raise OSError("simulated crash before live swap")
        replace(source, destination)

    monkeypatch.setattr(sync_module.os, "replace", fail_live_swap)

    with pytest.raises(OSError, match="simulated"):
        await sync.check_once()

    assert (live / "cert.pem").read_text() == old_cert
    assert (live / "key.pem").read_text() == old_key


def test_certificate_poll_delay_has_bounded_jitter():
    assert sync_module._certificate_poll_delay(100, random_value=0.0) == 90
    assert sync_module._certificate_poll_delay(100, random_value=1.0) == 110


async def test_disabled_certificate_endpoint_is_a_noop(tmp_path):
    sync = CertificateSynchronizer(
        FakeClient(None),
        certificate_path=tmp_path / "current/cert.pem",
        private_key_path=tmp_path / "current/key.pem",
        restart_notifier=lambda: pytest.fail("must not restart"),
    )
    assert (await sync.check_once()).status == "disabled"
