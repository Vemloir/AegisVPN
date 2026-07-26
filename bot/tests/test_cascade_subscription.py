from urllib.parse import unquote, urlsplit

from src.models import Server
from src.services.subscription_service import (
    _cascade_visible_servers,
    _replace_link_label,
)


def server(server_id: int, role: str) -> Server:
    return Server(
        id=server_id,
        name=f"node-{server_id}",
        flag="X",
        host="203.0.113.10",
        port=443,
        public_key="pk",
        short_id="sid",
        agent_url="http://127.0.0.1:8444",
        agent_token="legacy",
        node_role=role,
    )


def test_unacknowledged_entry_is_suppressed_without_affecting_direct_locations():
    entry = server(1, "entry")
    direct = server(2, "both")

    assert _cascade_visible_servers([entry, direct], {}) == [direct]
    assert _cascade_visible_servers(
        [entry, direct],
        {entry.id: "Russia → Germany | Frankfurt"},
    ) == [entry, direct]


def test_advertised_entry_uses_explicit_path_label():
    link = "vless://uuid@203.0.113.10:443?type=xhttp#Russia"
    replaced = _replace_link_label(link, "Russia → Germany | Frankfurt")
    assert unquote(urlsplit(replaced).fragment) == "Russia → Germany | Frankfurt"
