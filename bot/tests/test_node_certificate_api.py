import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from src.api.main import app
from src.core.config import settings
from src.core.database import async_session_maker, engine
from src.models import Base, Server

PROXY_SECRET = "certificate-proxy-secret"
NODE_TOKEN = "certificate-node-token"
NODE_FINGERPRINT = "aabb1122ccdd3344"
HOSTNAME = "hy2.example.test"


def _certificate_pair(hostname: str = HOSTNAME) -> tuple[bytes, bytes]:
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
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _headers(token: str = NODE_TOKEN) -> dict[str, str]:
    return {
        "X-Aegis-Proxy-Secret": PROXY_SECRET,
        "X-Aegis-Node-Fingerprint": NODE_FINGERPRINT,
        "Authorization": f"Bearer {token}",
    }


@pytest.fixture(autouse=True)
async def _schema(tmp_path, monkeypatch):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    cert, key = _certificate_pair()
    (tmp_path / "cert.pem").write_bytes(cert)
    (tmp_path / "key.pem").write_bytes(key)
    (tmp_path / "hostname").write_text(HOSTNAME)
    monkeypatch.setitem(settings.__dict__, "node_control_proxy_secret", SecretStr(PROXY_SECRET))
    monkeypatch.setitem(settings.__dict__, "node_hy2_certificate_dir", str(tmp_path))

    async with async_session_maker() as session:
        session.add(
            Server(
                name="Certificate node",
                flag="C",
                host="203.0.113.88",
                port=443,
                public_key="pk",
                short_id="sid",
                agent_url="http://127.0.0.1:8444",
                agent_token="legacy",
                control_mode="observe",
                control_token_hash=hashlib.sha256(NODE_TOKEN.encode()).hexdigest(),
                control_cert_fingerprint=NODE_FINGERPRINT,
                is_active=True,
                hy2_enabled=True,
                hy2_port=443,
                hy2_sni=HOSTNAME,
            )
        )
        await session.commit()


async def test_certificate_endpoint_requires_node_auth_and_returns_no_store_bundle():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.get("/api/node/v1/hy2-certificate")
        assert anonymous.status_code == 401
        assert anonymous.headers["cache-control"] == "no-store"
        response = await client.get("/api/node/v1/hy2-certificate", headers=_headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["hostname"] == HOSTNAME
    assert body["certificate"].startswith("-----BEGIN CERTIFICATE-----")
    assert body["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert len(body["fingerprint"]) == 64


async def test_disabled_or_hostname_mismatched_node_cannot_receive_bundle():
    async with async_session_maker() as session:
        node = await session.get(Server, 1)
        node.hy2_enabled = False
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/node/v1/hy2-certificate", headers=_headers())).status_code == 404

    async with async_session_maker() as session:
        node = await session.get(Server, 1)
        node.hy2_enabled = True
        node.hy2_sni = "other.example.test"
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/node/v1/hy2-certificate", headers=_headers())).status_code == 409


async def test_shared_legacy_bundle_is_rejected_when_sni_is_used_by_multiple_nodes():
    async with async_session_maker() as session:
        session.add(
            Server(
                name="Second certificate node",
                flag="C",
                host="203.0.113.89",
                port=443,
                public_key="pk2",
                short_id="sid2",
                agent_url="http://127.0.0.1:8444",
                agent_token="legacy2",
                control_mode="observe",
                control_token_hash=hashlib.sha256(b"other-token").hexdigest(),
                control_cert_fingerprint="eeff1122ccdd3344",
                is_active=True,
                hy2_enabled=True,
                hy2_port=443,
                hy2_sni=HOSTNAME,
            )
        )
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/node/v1/hy2-certificate", headers=_headers())

    assert response.status_code == 409
    assert response.json()["detail"] == "Per-node Hy2 certificate required"
