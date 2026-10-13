import os
import re
from pathlib import Path

from cryptography import x509

from app import connlimit
from app.certificate_sync import CertificateSynchronizer, install_placeholder_certificate
from app.xray import find_vless_inbound

AGENT = Path(__file__).resolve().parents[1]


# --- entrypoint: the CDN origin inbound ---------------------------------------


def cdn_xhttp_profile() -> dict:
    src = (AGENT / "entrypoint.sh").read_text()
    literal = re.search(r"^CDN_XHTTP_PROFILE = (\{.*?^\})", src, re.S | re.M)
    assert literal, "CDN_XHTTP_PROFILE not found in entrypoint.sh"
    scope: dict = {}
    exec(f"profile = {literal.group(1)}", scope)
    return scope["profile"]


def _build_cdn_inbound(monkeypatch, tmp_path, clients=None):
    src = (AGENT / "entrypoint.sh").read_text()
    fn = re.search(r"(def build_cdn_inbound\(port: int\) -> dict \| None:.*?)\n\n\n", src, re.S)
    assert fn, "build_cdn_inbound not found in entrypoint.sh"
    monkeypatch.setenv("CDN_CERTIFICATE_PATH", str(tmp_path / "cdn" / "current" / "cert.pem"))
    monkeypatch.setenv("CDN_PRIVATE_KEY_PATH", str(tmp_path / "cdn" / "current" / "key.pem"))
    monkeypatch.setenv("XRAY_CDN_PATH", "/media/")
    scope: dict = {
        "os": os,
        "CDN_XHTTP_PROFILE": cdn_xhttp_profile(),
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
    assert stream["xhttpSettings"] == {"path": "/media/", "mode": "auto", **cdn_xhttp_profile()}
    # Nothing a CDN needs may live in the path: it forwards file-like paths only.
    assert stream["xhttpSettings"]["sessionIDPlacement"] == "query"
    assert stream["xhttpSettings"]["seqPlacement"] == "query"
    # Padding never goes into a URL-shaped Referer, which the CDN filter flags.
    assert stream["xhttpSettings"]["xPaddingObfsMode"] is True
    certificate = stream["tlsSettings"]["certificates"][0]
    assert Path(certificate["certificateFile"]).exists()
    assert Path(certificate["keyFile"]).exists()
    # Same users as every other inbound, never with the vision flow, under
    # their own "_cdn" email so Xray counts CDN traffic separately.
    assert inbound["settings"]["clients"] == [{"id": "u1", "email": "user_1_sub_1_cdn"}]


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


async def test_loopback_sources_are_neither_counted_nor_blocked(monkeypatch):
    from tests.test_connlimit import _patch_enforce

    blocked, _ = _patch_enforce(
        monkeypatch,
        xray_users=["user_1_sub_1"],
        # Behind the REALITY SNI split every CDN user shows up from loopback.
        xray_ips={"user_1_sub_1": {"10.0.0.1": 100, "10.0.0.2": 200, "127.0.0.1": 300, "::1": 400}},
        hy2_counts={},
        limit=2,
    )
    monkeypatch.setattr(connlimit, "cdn_peer_ips", lambda: set())
    assert await connlimit.enforce_conn_limit_once() == 0
    assert not any(blocked)


# --- entrypoint: REALITY and the CDN origin sharing 443 by SNI -----------------


def _ensure_cdn_sni_split(monkeypatch, host="n22.example.dev"):
    src = (AGENT / "entrypoint.sh").read_text()
    fn = re.search(r"(def ensure_cdn_sni_split\(cfg: dict\) -> None:.*?)\n\n\n", src, re.S)
    assert fn, "ensure_cdn_sni_split not found in entrypoint.sh"
    if host is None:
        monkeypatch.delenv("XRAY_CDN_HOST", raising=False)
    else:
        monkeypatch.setenv("XRAY_CDN_HOST", host)
    monkeypatch.setenv("REALITY_DEST", "www.example.org:443")
    monkeypatch.delenv("XRAY_CDN_SPLIT_PORT", raising=False)

    def _int_env(name, default):
        try:
            return int((os.environ.get(name) or "").strip())
        except ValueError:
            return default

    scope: dict = {"os": os, "_int_env": _int_env}
    exec(fn.group(1), scope)
    return scope["ensure_cdn_sni_split"]


def _split_config() -> dict:
    reality = {"dest": "www.example.org:443", "serverNames": ["www.example.org"]}
    return {
        "inbounds": [
            {"tag": "vless-in", "port": 443, "streamSettings": {"security": "reality", "realitySettings": dict(reality)}},
            {"tag": "vless-in-tcp", "port": 9445, "streamSettings": {"security": "reality", "realitySettings": dict(reality)}},
            {"tag": "vless-in-cdn", "port": 8443, "streamSettings": {"security": "tls"}},
            {"tag": "api", "listen": "127.0.0.1", "port": 10085, "protocol": "dokodemo-door"},
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
        "routing": {
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
            ]
        },
    }


def test_sni_split_hands_the_origin_host_to_the_loopback_cdn_inbound(monkeypatch):
    cfg = _split_config()
    _ensure_cdn_sni_split(monkeypatch)(cfg)
    inbounds = {i["tag"]: i for i in cfg["inbounds"]}

    # Every REALITY inbound now falls back to the local splitter, not the site.
    assert inbounds["vless-in"]["streamSettings"]["realitySettings"]["dest"] == "127.0.0.1:10443"
    assert inbounds["vless-in-tcp"]["streamSettings"]["realitySettings"]["dest"] == "127.0.0.1:10443"
    # The CDN inbound is no longer reachable from outside.
    assert inbounds["vless-in-cdn"]["listen"] == "127.0.0.1"

    split = inbounds["reality-split"]
    assert split["listen"] == "127.0.0.1" and split["port"] == 10443
    assert split["settings"] == {"address": "www.example.org", "port": 443, "network": "tcp"}
    assert split["sniffing"] == {"enabled": True, "destOverride": ["tls"], "routeOnly": True}

    origin = next(o for o in cfg["outbounds"] if o["tag"] == "cdn-origin")
    assert origin["settings"] == {"redirect": "127.0.0.1:8443"}

    rules = cfg["routing"]["rules"]
    assert rules[0]["inboundTag"] == ["api"]
    # Split rules come before the private-network block: REALITY_DEST is public
    # but the redirect target is loopback.
    assert rules[1] == {
        "type": "field",
        "inboundTag": ["reality-split"],
        "domain": ["full:n22.example.dev"],
        "outboundTag": "cdn-origin",
    }
    assert rules[2] == {"type": "field", "inboundTag": ["reality-split"], "outboundTag": "direct"}
    assert rules[3]["ip"] == ["geoip:private"]


def test_sni_split_is_idempotent(monkeypatch):
    cfg = _split_config()
    ensure = _ensure_cdn_sni_split(monkeypatch)
    ensure(cfg)
    ensure(cfg)
    assert [i["tag"] for i in cfg["inbounds"]].count("reality-split") == 1
    assert [o["tag"] for o in cfg["outbounds"]].count("cdn-origin") == 1
    assert sum(r.get("inboundTag") == ["reality-split"] for r in cfg["routing"]["rules"]) == 2


def test_sni_split_is_removed_without_an_origin_host(monkeypatch):
    cfg = _split_config()
    _ensure_cdn_sni_split(monkeypatch)(cfg)
    # Next boot: inbounds are rebuilt fresh, the host is gone.
    cfg["inbounds"] = _split_config()["inbounds"] + [i for i in cfg["inbounds"] if i["tag"] == "reality-split"]
    _ensure_cdn_sni_split(monkeypatch, host=None)(cfg)

    assert "reality-split" not in [i["tag"] for i in cfg["inbounds"]]
    assert "cdn-origin" not in [o["tag"] for o in cfg["outbounds"]]
    assert not any(r.get("inboundTag") == ["reality-split"] for r in cfg["routing"]["rules"])
    inbounds = {i["tag"]: i for i in cfg["inbounds"]}
    assert inbounds["vless-in"]["streamSettings"]["realitySettings"]["dest"] == "www.example.org:443"
    assert "listen" not in inbounds["vless-in-cdn"]


def test_sni_split_needs_a_cdn_inbound(monkeypatch):
    cfg = _split_config()
    cfg["inbounds"] = [i for i in cfg["inbounds"] if i["tag"] != "vless-in-cdn"]
    _ensure_cdn_sni_split(monkeypatch)(cfg)
    assert "reality-split" not in [i["tag"] for i in cfg["inbounds"]]
    assert cfg["inbounds"][0]["streamSettings"]["realitySettings"]["dest"] == "www.example.org:443"


# --- CDN traffic gets its own email ---------------------------------------------


def test_cdn_inbound_clients_get_the_cdn_email_and_others_do_not():
    from app.xray import CDN_INBOUND_TAG, build_client_record

    cdn = build_client_record("u1", "user_1_sub_2_dev_3", {"tag": CDN_INBOUND_TAG, "streamSettings": {"network": "xhttp"}})
    direct = build_client_record("u1", "user_1_sub_2_dev_3", {"tag": "vless-in", "streamSettings": {"network": "xhttp"}})
    assert cdn["email"] == "user_1_sub_2_dev_3_cdn"
    assert direct["email"] == "user_1_sub_2_dev_3"
    # Idempotent: a record rebuilt from an already-suffixed email is unchanged.
    assert build_client_record("u1", cdn["email"], {"tag": CDN_INBOUND_TAG})["email"] == cdn["email"]


def test_hysteria_and_online_state_use_the_subscribers_email(monkeypatch):
    import asyncio

    from app import hysteria, xray

    hysteria.refresh_from_config(
        {
            "inbounds": [
                {"protocol": "vless", "tag": "vless-in", "settings": {"clients": [{"id": "u1", "email": "user_1_sub_1"}]}},
                {
                    "protocol": "vless",
                    "tag": xray.CDN_INBOUND_TAG,
                    "settings": {"clients": [{"id": "u1", "email": "user_1_sub_1_cdn"}]},
                },
            ]
        }
    )
    # Hy2 traffic must never be booked as CDN traffic.
    assert hysteria.authenticate("u1") == {"ok": True, "id": "user_1_sub_1"}

    async def fake_run(*_args, **_kwargs):
        return 0, b'{"users": {"user_1_sub_1": 1, "user_1_sub_1_cdn": 1, "user_2_sub_5_cdn": 1}}', b""

    monkeypatch.setattr(xray, "_run_process", fake_run)
    assert asyncio.run(xray.get_online_emails()) == ["user_1_sub_1", "user_2_sub_5"]
