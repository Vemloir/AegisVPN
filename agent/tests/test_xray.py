from app.xray import _parse_online_users, build_client_record, build_subscription_query, get_transport_type


def test_get_transport_type():
    assert get_transport_type({"streamSettings": {"network": "tcp"}}) == "tcp"
    assert get_transport_type({"streamSettings": {"network": "xhttp"}}) == "xhttp"
    assert get_transport_type({"streamSettings": {"network": "grpc"}}) == "grpc"
    assert get_transport_type({}) == "tcp"  # default


def test_build_client_record_tcp_has_no_flow():
    # vision is gone: raw-tcp clients are now flow-less REALITY.
    record = build_client_record("uuid-1", "user@x", {"streamSettings": {"network": "tcp"}})
    assert record["id"] == "uuid-1"
    assert record["email"] == "user@x"
    assert "flow" not in record


def test_build_client_record_grpc_has_no_flow():
    record = build_client_record("uuid-3", "user@x", {"streamSettings": {"network": "grpc"}})
    assert record["id"] == "uuid-3"
    assert "flow" not in record


def test_build_client_record_xhttp_has_no_flow():
    record = build_client_record("uuid-2", "user@x", {"streamSettings": {"network": "xhttp"}})
    assert "flow" not in record


def test_build_subscription_query_tcp():
    inbound = {
        "streamSettings": {
            "network": "tcp",
            "realitySettings": {"serverNames": ["example.com"]},
        }
    }
    query = dict(build_subscription_query(inbound))
    assert query["type"] == "tcp"
    assert query["security"] == "reality"
    assert query["headerType"] == "none"
    assert "flow" not in query
    assert query["sni"] == "example.com"


def test_build_subscription_query_grpc():
    inbound = {
        "streamSettings": {
            "network": "grpc",
            "grpcSettings": {"serviceName": "mysvc"},
            "realitySettings": {"serverNames": ["example.com"]},
        }
    }
    query = dict(build_subscription_query(inbound))
    assert query["type"] == "grpc"
    assert query["security"] == "reality"
    assert query["serviceName"] == "mysvc"
    assert query["mode"] == "gun"
    assert "flow" not in query
    assert query["sni"] == "example.com"


def test_build_subscription_query_xhttp():
    inbound = {
        "streamSettings": {
            "network": "xhttp",
            "xhttpSettings": {"path": "/p", "mode": "packet-up"},
            "realitySettings": {"serverNames": ["cdn.example.com"]},
        }
    }
    query = dict(build_subscription_query(inbound))
    assert query["type"] == "xhttp"
    assert "flow" not in query
    assert query["path"] == "/p"
    assert query["mode"] == "packet-up"


def test_build_subscription_query_xhttp_mode_defaults_to_auto():
    # When the live inbound carries no explicit xhttp mode, the sub link falls
    # back to the settings default, which is now "auto" (client resolves auto to
    # stream-one over direct REALITY; server-side auto accepts every mode).
    inbound = {
        "streamSettings": {
            "network": "xhttp",
            "xhttpSettings": {"path": "/"},
            "realitySettings": {"serverNames": ["cdn.example.com"]},
        }
    }
    query = dict(build_subscription_query(inbound))
    assert query["mode"] == "auto"


def test_parse_online_users_dict():
    raw = b'{"users": {"user_1_sub_2_dev_3": ["1.2.3.4"], "user_1_sub_2": ["5.6.7.8", "9.9.9.9"]}}'
    emails = set(_parse_online_users(raw))
    assert emails == {"user_1_sub_2_dev_3", "user_1_sub_2"}


def test_parse_online_users_list_of_strings():
    raw = b'{"users": ["a@x", "b@x"]}'
    assert set(_parse_online_users(raw)) == {"a@x", "b@x"}


def test_parse_online_users_list_of_records():
    raw = b'{"users": [{"email": "a@x"}, {"user": "b@x"}]}'
    assert set(_parse_online_users(raw)) == {"a@x", "b@x"}


def test_parse_online_users_empty_or_garbage():
    assert _parse_online_users(b"") == []
    assert _parse_online_users(b"not json") == []
    assert _parse_online_users(b'{"users": null}') == []
    assert _parse_online_users(b"{}") == []
