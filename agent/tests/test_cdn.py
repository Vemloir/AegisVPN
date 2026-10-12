import os
import re
from pathlib import Path

from cryptography import x509

from app import connlimit
from app.certificate_sync import CertificateSynchronizer, install_placeholder_certificate
from app.xray import find_vless_inbound

AGENT = Path(__file__).resolve().parents[1]


# --- entrypoint: the CDN origin inbound ---------------------------------------


def _build_cdn_inbound(monkeypatch, tmp_path, clients=None):
    src = (AGENT / "entrypoint.sh").read_text()
    fn = re.search(r"(def build_cdn_inbound\(port: int\) -> dict \| None:.*?)\n\n\n", src, re.S)
    assert fn, "build_cdn_inbound not found in entrypoint.sh"
    monkeypatch.setenv("CDN_CERTIFICATE_PATH", str(tmp_path / "cdn" / "current" / "cert.pem"))
    monkeypatch.setenv("CDN_PRIVATE_KEY_PATH", str(tmp_path / "cdn" / "current" / "key.pem"))
    monkeypatch.setenv("XRAY_CDN_PATH", "/media/")
    scope: dict = {
        "os": os,
        "client_map": clients or {},
        "keepalive_idle": 20,
        "keepalive_interval": 10,
    }
    exec(fn.group(1), scope)
    return scope["build_cdn_inbound"]


def test_cdn_inbound_is_plain_tls_xhttp_with_shared_clients(monkeypatch, tmp_path):
    clients = {"u1": {"id": "u1", "email": "user_1_sub_1"}}
    inbound = _build_cdn_inbound(monkeypatch, tmp_path, clients)(8443)

    assert inbound["tag"] == "vless-in-cdn"
    assert inbound["port"] == 8443
    stream = inbound["streamSettings"]
    assert stream["network"] == "xhttp"
    assert stream["security"] == "tls"
    assert "realitySettings" not in stream
    assert stream["xhttpSettings"] == {"path": "/media/", "mode": "auto"}
    certificate = stream["tlsSettings"]["certificates"][0]
    assert Path(certificate["certificateFile"]).exists()
    assert Path(certificate["keyFile"]).exists()
    # Same users as every other inbound, never with the vision flow.
    assert inbound["settings"]["clients"] == [{"id": "u1", "email": "user_1_sub_1"}]


def test_cdn_inbound_keeps_an_existing_certificate(monkeypatch, tmp_path):
    build = _build_cdn_inbound(monkeypatch, tmp_path)
    first = build(8443)
    cert_path = Path(first["streamSettings"]["tlsSettings"]["certificates"][0]["certificateFile"])
    before = cert_path.read_bytes()
    build(8443)
    assert cert_path.read_bytes() == before


# --- certificates ---------------------------------------------------------------


def test_placeholder_is_installed_once_as_atomic_symlink(tmp_path):
    cert = tmp_path / "cdn" / "current" / "cert.pem"
    key = tmp_path / "cdn" / "current" / "key.pem"
    assert install_placeholder_certificate(cert, key) is True
    assert (tmp_path / "cdn" / "current").is_symlink()
    x509.load_pem_x509_certificate(cert.read_bytes())
    assert install_placeholder_certificate(cert, key) is False


async def test_synchronizer_can_track_the_cdn_bundle(tmp_path):
    calls = []

    class Client:
        async def get_hy2_certificate(self):  # pragma: no cover - must not be used
            raise AssertionError("Hy2 bundle requested by the CDN synchronizer")

    async def fetch_cdn():
        calls.append("cdn")
        return None

    synchronizer = CertificateSynchronizer(
        Client(),
        certificate_path=tmp_path / "current" / "cert.pem",
        private_key_path=tmp_path / "current" / "key.pem",
        fetch=fetch_cdn,
    )
    result = await synchronizer.check_once()
    assert result.status == "disabled"
    assert calls == ["cdn"]


async def test_real_certificate_replaces_the_placeholder(tmp_path):
    from tests.test_certificate_sync import _bundle, _pair

    cert_path = tmp_path / "current" / "cert.pem"
    key_path = tmp_path / "current" / "key.pem"
    install_placeholder_certificate(cert_path, key_path)
    cert, key = _pair()
    notified = []

    async def fetch():
        return _bundle(cert, key)

    synchronizer = CertificateSynchronizer(
        object(),
        certificate_path=cert_path,
        private_key_path=key_path,
        restart_notifier=lambda: notified.append(True),
        fetch=fetch,
    )
    result = await synchronizer.check_once()
    assert result.status == "updated"
    assert cert_path.read_text() == cert
    assert notified == [True]


# --- subscription links never come from the CDN inbound -------------------------


def test_direct_links_skip_the_cdn_inbound():
    config = {
        "inbounds": [
            {"tag": "vless-in", "protocol": "vless", "streamSettings": {"network": "tcp"}},
            {"tag": "vless-in-cdn", "protocol": "vless", "streamSettings": {"network": "xhttp"}},
        ]
    }
    # No direct xhttp inbound: fall back to the primary, never to the CDN one.
    assert find_vless_inbound(config, preferred_network="xhttp")["tag"] == "vless-in"


# --- conn-limit never counts or blocks a CDN edge -------------------------------


def _proc_line(local: str, remote: str, state: str = "01") -> str:
    return f"   0: {local} {remote} {state} 00000000:00000000 00:00000000 00000000     0        0 1 1 0 0 0 0 0\n"


def test_cdn_peer_ips_reads_established_sockets_on_the_cdn_port(tmp_path):
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (tmp_path / "tcp").write_text(
        header
        # 0.0.0.0:8443 listening -> ignored
        + _proc_line("00000000:20FB", "00000000:0000", "0A")
        # 10.0.0.5:8443 <- 91.240.169.224 established -> the edge
        + _proc_line("0500000A:20FB", "E0A9F05B:C350")
        # an established connection on another port -> ignored
        + _proc_line("0500000A:01BB", "04030201:C351")
    )
    (tmp_path / "tcp6").write_text(
        header
        # [::ffff:10.0.0.5]:8443 <- [::ffff:91.238.111.224] -> reported as IPv4
        + _proc_line(
            "0000000000000000FFFF00000500000A:20FB",
            "0000000000000000FFFF0000E06FEE5B:C352",
        )
    )
    assert connlimit.cdn_peer_ips(8443, proc_root=str(tmp_path)) == {"91.240.169.224", "91.238.111.224"}


def test_cdn_peer_ips_is_empty_without_a_cdn_port(tmp_path):
    assert connlimit.cdn_peer_ips(0, proc_root=str(tmp_path)) == set()


async def test_cdn_edges_are_neither_counted_nor_blocked(monkeypatch):
    from tests.test_connlimit import _patch_enforce

    blocked, _ = _patch_enforce(
        monkeypatch,
        xray_users=["user_1_sub_1"],
        # Two direct devices plus two CDN edges: over a limit of 2 only if the
        # edges were counted.
        xray_ips={"user_1_sub_1": {"10.0.0.1": 100, "10.0.0.2": 200, "91.240.169.224": 300, "91.238.111.224": 400}},
        hy2_counts={},
        limit=2,
    )
    monkeypatch.setattr(connlimit, "cdn_peer_ips", lambda: {"91.240.169.224", "91.238.111.224"})
    assert await connlimit.enforce_conn_limit_once() == 0
    assert not any(blocked)
