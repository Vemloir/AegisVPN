from datetime import UTC, datetime, timedelta

from src.models import Server, Subscription
from src.services import SubscriptionService

# A raw vless link as the agent returns it from /sub/<uuid> (xhttp default
# inbound). The bot's normalize_vless_uri rewrites it onto the bot's
# authoritative host/port + reality keypair.
_AGENT_RAW = (
    "vless://0146ca3d-e9b9-459a-8e54-b611dc601bec@agent-host:443?type=xhttp"
    "&security=reality&encryption=none&sni=gateway.icloud.com&fp=firefox"
    "&pbk=AGENTPBK&sid=AGENTSID&path=%2Fgr-xh&mode=auto#Greece"
)


def _greece_server() -> Server:
    return Server(
        id=1,
        name="Greece",
        flag="\U0001F1EC\U0001F1F7",
        host="45.142.31.13",
        port=443,
        public_key="GRPBK",
        short_id="GRSID",
        tcp_port=2053,
    )


def _xhttp_only_server() -> Server:
    return Server(
        id=2,
        name="Finland",
        flag="\U0001F1EB\U0001F1EE",
        host="1.2.3.4",
        port=443,
        public_key="FIPBK",
        short_id="FISID",
    )


def _sub(**kwargs) -> Subscription:
    return Subscription(**kwargs)


def test_format_server_label_drops_city():
    # City names were removed from VPN labels: "Country | City" renders as the
    # country only. The stored name keeps the city (admin/host clarity).
    s = Server(id=8, name="Germany | Frankfurt", flag="\U0001F1E9\U0001F1EA",
               host="h", port=443, public_key="p", short_id="s")
    assert SubscriptionService.server_country(s) == "Germany"
    assert SubscriptionService.format_server_label(s) == "\U0001F1E9\U0001F1EA Germany"
    # A name without a city is unchanged.
    assert SubscriptionService.server_country(
        Server(id=9, name="Germany", flag="x", host="h", port=443, public_key="p", short_id="s")
    ) == "Germany"


def test_duplicate_suffix_keys_on_country_not_city():
    from collections import Counter
    de1 = Server(id=8, name="Germany | Frankfurt", flag="\U0001F1E9\U0001F1EA",
                 host="h1", port=443, public_key="p", short_id="s")
    de2 = Server(id=20, name="Germany | Munich", flag="\U0001F1E9\U0001F1EA",
                 host="h2", port=443, public_key="p", short_id="s")
    fi = Server(id=1, name="Finland | Helsinki", flag="\U0001F1EB\U0001F1EE",
                host="h3", port=443, public_key="p", short_id="s")
    counts = Counter(SubscriptionService.server_country(x).casefold() for x in (de1, de2, fi))
    dup = {n for n, c in counts.items() if c > 1}
    # Two Germanys (different cities) collide on country -> both get the №id.
    assert SubscriptionService.format_server_label(de1, dup) == "\U0001F1E9\U0001F1EA Germany №8"
    assert SubscriptionService.format_server_label(de2, dup) == "\U0001F1E9\U0001F1EA Germany №20"
    # Finland is unique -> no suffix.
    assert SubscriptionService.format_server_label(fi, dup) == "\U0001F1EB\U0001F1EE Finland"


def test_is_lifetime_by_plan_days():
    sub = _sub(plan_days=SubscriptionService.LIFETIME_PLAN_DAYS, expires_at=datetime(2030, 1, 1))
    assert SubscriptionService.is_lifetime_subscription(sub)


def test_is_lifetime_by_expires_at():
    sub = _sub(plan_days=30, expires_at=SubscriptionService.LIFETIME_EXPIRES_AT)
    assert SubscriptionService.is_lifetime_subscription(sub)


def test_regular_subscription_is_not_lifetime():
    expires = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)
    sub = _sub(plan_days=30, expires_at=expires)
    assert not SubscriptionService.is_lifetime_subscription(sub)


def test_none_is_not_lifetime():
    assert not SubscriptionService.is_lifetime_subscription(None)


def test_generate_sub_token_value_is_unique():
    seen: set[str] = set()
    for _ in range(200):
        token = SubscriptionService.generate_sub_token_value(seen)
        assert token and token not in seen
        seen.add(token)


def test_build_subscription_url_contains_token():
    url = SubscriptionService.build_subscription_url("tok123")
    assert url.startswith("http")
    assert "/sub/tok123" in url


def test_happ_android_build_number_is_not_os_version():
    # Happ appends its own build number after the OS name; it must not be read as
    # an Android version (regression: "Android 17800541067281831514").
    ua = "Happ/2.9.1/Android/17800541067281831514"
    assert SubscriptionService._detect_platform(ua) == "Android"
    assert SubscriptionService.make_device_display_name(ua) == "Android · Happ"


def test_standard_android_version_is_parsed():
    ua = "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36"
    assert SubscriptionService._detect_platform(ua) == "Android 14"


def test_happ_windows_has_no_bogus_version():
    assert SubscriptionService._detect_platform("Happ/2.9.1/Windows/2604241012503") == "Windows"


def test_ios_version_parsed_but_build_rejected():
    assert SubscriptionService._detect_platform("Happ/2.9.1/iPhone/99887766554433") == "iPhone"
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)"
    assert SubscriptionService._detect_platform(ua) == "iPhone · iOS 17"


def test_extract_build_from_happ_ua():
    assert SubscriptionService.extract_build("Happ/2.9.1/Android/17800541067281831514") == "17800541067281831514"
    assert SubscriptionService.extract_build("Happ/2.9.1/Windows/2604241012503") == "2604241012503"


def test_extract_build_none_when_absent():
    assert SubscriptionService.extract_build("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)") is None
    assert SubscriptionService.extract_build("v2rayNG/1.8.5") is None


def test_vless_link_to_xray_config_xhttp_has_recovery_knobs_and_clean_routing():
    link = (
        "vless://0146ca3d-e9b9-459a-8e54-b611dc601bec@1.2.3.4:443?type=xhttp"
        "&security=reality&encryption=none&sni=gateway.icloud.com&fp=firefox"
        "&pbk=PUBKEY&sid=SID&path=%2Ffi-xh&mode=auto#%F0%9F%87%AB%F0%9F%87%AE Finland"
    )
    cfg = SubscriptionService._vless_link_to_xray_config(link, Server(name="Finland | Helsinki"))
    assert cfg is not None
    # proxy first => unmatched traffic falls through to the tunnel (default-proxy)
    assert [o["tag"] for o in cfg["outbounds"]] == ["proxy", "direct", "block"]
    ss = cfg["outbounds"][0]["streamSettings"]
    assert ss["network"] == "xhttp"
    assert ss["xhttpSettings"]["path"] == "/fi-xh"
    # roaming-recovery knobs are present
    assert ss["xhttpSettings"]["xmux"]["hKeepAlivePeriod"] == 15
    assert ss["sockopt"]["tcpKeepAliveIdle"] == 10
    assert ss["realitySettings"]["publicKey"] == "PUBKEY"
    assert cfg["outbounds"][0]["settings"]["vnext"][0]["users"][0]["id"] == "0146ca3d-e9b9-459a-8e54-b611dc601bec"
    # clean iOS-safe routing: ru/cn direct via a static suffix list, AsIs so the
    # geo .dat files are never loaded (they blow the iOS ~50 MB tunnel cap).
    assert cfg["routing"]["domainStrategy"] == "AsIs"
    ru_cn = {d for r in cfg["routing"]["rules"] for d in r.get("domain", [])}
    assert {"domain:ru", "domain:cn"} <= ru_cn
    assert not any(
        "geosite" in v or "geoip" in v
        for r in cfg["routing"]["rules"]
        for v in (r.get("domain", []) + r.get("ip", []))
    )
    # DNS resolves DIRECT (https+local), never through the proxy -> recovery works
    assert all(
        (s if isinstance(s, str) else s["address"]).startswith("https+local://")
        for s in cfg["dns"]["servers"]
    )
    assert cfg["remarks"] == "🇫🇮 Finland"


# --- per-location transport selection ---------------------------------------


def test_default_transport_is_byte_identical():
    """A user with no preference (transport=None) gets EXACTLY the same link as
    before this change: the legacy code path used transport=None implicitly."""
    server = _greece_server()
    # `None` is what every server without a pref resolves to in _collect_links.
    with_none = SubscriptionService.normalize_vless_uri(_AGENT_RAW, server, transport=None)
    # Explicitly asking for xhttp must produce the identical string too.
    with_xhttp = SubscriptionService.normalize_vless_uri(_AGENT_RAW, server, transport="xhttp")
    assert with_none == with_xhttp
    # And it is the xhttp link on port 443 with the bot's reality keypair.
    assert "type=xhttp" in with_none
    assert "@45.142.31.13:443" in with_none
    assert "pbk=GRPBK" in with_none and "sid=GRSID" in with_none
    assert "flow=" not in with_none


def test_tcp_transport_uses_tcp_port_and_vision_flow():
    server = _greece_server()
    link = SubscriptionService.normalize_vless_uri(_AGENT_RAW, server, transport="tcp")
    assert "type=tcp" in link
    assert "@45.142.31.13:2053" in link  # server.tcp_port
    assert "headerType=none" in link
    # Greece tcp alt-transport runs TCP+REALITY with the vision flow (must match
    # the agent's vless-in-tcp inbound clients).
    assert "flow=xtls-rprx-vision" in link
    cfg = SubscriptionService._vless_link_to_xray_config(link, server)
    stream = cfg["outbounds"][0]["streamSettings"]
    assert stream["network"] == "tcp"
    assert cfg["outbounds"][0]["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"
    # No Mux/xudp anywhere — Mux breaks the vision flow.
    assert "mux" not in cfg["outbounds"][0]
    assert "xudp" not in cfg["outbounds"][0]["settings"]


def test_xhttp_only_server_offers_no_transport_choice():
    server = _xhttp_only_server()
    assert server.has_alt_transports is False
    assert SubscriptionService.available_transports(server) == ["xhttp"]


def test_greece_offers_xhttp_and_tcp_transports():
    server = _greece_server()
    assert server.has_alt_transports is True
    assert SubscriptionService.available_transports(server) == ["xhttp", "tcp"]


def test_hy2_pref_falls_back_to_xhttp():
    server = _greece_server()
    # resolve_transport collapses an hy2 pref to the default xhttp (no backend).
    assert SubscriptionService.resolve_transport(server, "hy2", "xhttp") == "xhttp"
    assert SubscriptionService.resolve_transport(server, "hy2", "tcp") == "xhttp"
    # A tcp pref on a server that lost the capability also falls back.
    assert SubscriptionService.resolve_transport(_xhttp_only_server(), "vless", "tcp") == "xhttp"
    # A valid tcp pref resolves to tcp.
    assert SubscriptionService.resolve_transport(server, "vless", "tcp") == "tcp"
