import asyncio

from app import xray
from app.xray import _parse_online_users, build_client_record, build_subscription_query, get_transport_type


def test_get_transport_type():
    assert get_transport_type({"streamSettings": {"network": "tcp"}}) == "tcp"
    assert get_transport_type({"streamSettings": {"network": "xhttp"}}) == "xhttp"
    assert get_transport_type({"streamSettings": {"network": "grpc"}}) == "grpc"
    assert get_transport_type({}) == "tcp"  # default


def test_build_client_record_tcp_has_vision_flow():
    # tcp/REALITY clients carry xtls-rprx-vision (must match the bot's tcp link).
    record = build_client_record("uuid-1", "user@x", {"streamSettings": {"network": "tcp"}})
    assert record["id"] == "uuid-1"
    assert record["email"] == "user@x"
    assert record["flow"] == "xtls-rprx-vision"


def test_build_client_record_default_network_has_vision_flow():
    # No streamSettings -> get_transport_type defaults to tcp -> vision flow.
    record = build_client_record("uuid-0", "user@x", {})
    assert record["flow"] == "xtls-rprx-vision"


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
    assert query["flow"] == "xtls-rprx-vision"
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


def test_parse_online_users_normalizes_pinned_stat_names():
    raw = b'{"users":["user>>>user_1_sub_2_dev_3>>>online"]}'
    assert _parse_online_users(raw) == ["user_1_sub_2_dev_3"]


def test_parse_online_users_discards_malformed_stat_names():
    raw = b'{"users":["user>>>secret>>>traffic", "a>>>b>>>c>>>d"]}'
    assert _parse_online_users(raw) == []


def test_parse_online_users_list_of_records():
    raw = b'{"users": [{"email": "a@x"}, {"user": "b@x"}]}'
    assert set(_parse_online_users(raw)) == {"a@x", "b@x"}


def test_parse_online_users_empty_or_garbage():
    assert _parse_online_users(b"") == []
    assert _parse_online_users(b"not json") == []
    assert _parse_online_users(b'{"users": null}') == []
    assert _parse_online_users(b"{}") == []


async def test_run_process_kills_and_reaps_after_timeout(monkeypatch):
    class HangingProcess:
        returncode = None

        def __init__(self):
            self.communicate_calls = 0
            self.killed = False

        async def communicate(self):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.sleep(60)
            self.returncode = -9
            return b"reaped", b""

        def kill(self):
            self.killed = True

    process = HangingProcess()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(xray.asyncio, "create_subprocess_exec", create_process)

    returncode, stdout, stderr = await xray._run_process(
        ["xray", "api", "statsquery"],
        timeout=0.01,
    )

    assert returncode == 124
    assert stdout == b"reaped"
    assert stderr == b""
    assert process.killed is True
    assert process.communicate_calls == 2
