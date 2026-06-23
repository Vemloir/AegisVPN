"""MTProto-proxy link emission: the mtproxy_capable gate + the
https://t.me/proxy link shape. The fake-TLS secret is opaque here (it already
encodes the camouflage domain); we only assert the link carries host/port/secret
and is suppressed until both the secret and the port are provisioned."""

from urllib.parse import parse_qs, urlsplit

from src.models import Server
from src.services import SubscriptionService


def _server(**overrides) -> Server:
    """A server with a fake-TLS mtg secret + listen port provisioned."""
    fields = {
        "name": "Greece",
        "flag": "GR",
        "host": "45.142.31.13",
        "port": 443,
        "public_key": "PBK",
        "short_id": "SID",
        "agent_url": "http://x",
        "agent_token": "t",
        # ee-prefixed fake-TLS secret (16 random bytes + the camouflage domain hex).
        "mtproxy_secret": "ee" + "ab" * 16 + "676f6f676c652e636f6d",
        "mtproxy_port": 8765,
    }
    fields.update(overrides)
    return Server(**fields)


def test_mtproxy_capable_requires_secret_and_port():
    assert _server().mtproxy_capable is True
    # Missing either half -> not capable (the bot then hands out no proxy link).
    assert _server(mtproxy_secret=None).mtproxy_capable is False
    assert _server(mtproxy_port=None).mtproxy_capable is False


def test_build_mtproxy_link_shape():
    link = SubscriptionService.build_mtproxy_link(_server())
    assert link is not None
    parts = urlsplit(link)
    assert parts.scheme == "https"
    assert parts.netloc == "t.me"
    assert parts.path == "/proxy"
    q = parse_qs(parts.query)
    assert q["server"] == ["45.142.31.13"]
    assert q["port"] == ["8765"]
    assert q["secret"][0].startswith("ee")  # fake-TLS secret carried verbatim


def test_build_mtproxy_link_none_when_not_capable():
    assert SubscriptionService.build_mtproxy_link(_server(mtproxy_secret=None)) is None
    assert SubscriptionService.build_mtproxy_link(_server(mtproxy_port=None)) is None
