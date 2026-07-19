"""The Telegram signature is the only thing between a visitor and someone else's
account: /api/auth/telegram accepts whatever JSON is POSTed to it."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, urlencode

from src.api.auth import (
    issue_session,
    read_session,
    verify_telegram_login,
    verify_telegram_webapp,
)
from src.core.config import settings


def _sign(payload: dict) -> dict:
    """Produce a payload signed exactly the way the Telegram widget signs one."""
    fields = {k: v for k, v in payload.items() if k != "hash"}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(settings.bot_token.get_secret_value().encode()).digest()
    return {**fields, "hash": hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()}


def _fresh(**overrides) -> dict:
    return _sign({"id": 4242, "username": "tester", "auth_date": int(time.time()), **overrides})


def test_valid_payload_yields_the_telegram_id():
    assert verify_telegram_login(_fresh()) == 4242


def test_unsigned_payload_is_rejected():
    assert verify_telegram_login({"id": 4242, "auth_date": int(time.time())}) is None
    assert verify_telegram_login({"id": 4242, "auth_date": int(time.time()), "hash": ""}) is None


def test_tampering_with_any_field_invalidates_the_signature():
    payload = _fresh()
    # The attacker keeps the captured signature but claims to be someone else.
    assert verify_telegram_login({**payload, "id": 1}) is None
    assert verify_telegram_login({**payload, "username": "admin"}) is None


def test_a_forged_hash_is_rejected():
    assert verify_telegram_login({**_fresh(), "hash": "0" * 64}) is None


def test_a_day_old_login_is_replayable_no_more():
    stale = _fresh(auth_date=int(time.time()) - 86_401)
    assert verify_telegram_login(stale) is None
    # ... while one just inside the window still works.
    assert verify_telegram_login(_fresh(auth_date=int(time.time()) - 60)) == 4242


def _sign_initdata(fields: dict) -> str:
    """Produce a Mini App initData string signed the way Telegram signs one."""
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", settings.bot_token.get_secret_value().encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _fresh_initdata(**overrides) -> str:
    user = json.dumps({"id": 4242, "username": "tester", "first_name": "Test"})
    return _sign_initdata({"user": user, "auth_date": str(int(time.time())), "query_id": "AA", **overrides})


def test_valid_initdata_yields_the_user():
    u = verify_telegram_webapp(_fresh_initdata())
    assert u is not None and u["id"] == 4242 and u["username"] == "tester"


def test_unsigned_or_incomplete_initdata_is_rejected():
    assert verify_telegram_webapp("") is None
    assert verify_telegram_webapp("user=%7B%22id%22%3A1%7D&auth_date=1") is None  # no hash


def test_tampering_with_initdata_invalidates_the_signature():
    pairs = dict(parse_qsl(_fresh_initdata(), keep_blank_values=True))
    # keep the captured hash but claim to be someone else
    pairs["user"] = json.dumps({"id": 1, "username": "admin"})
    assert verify_telegram_webapp(urlencode(pairs)) is None


def test_forged_webapp_hash_is_rejected():
    pairs = dict(parse_qsl(_fresh_initdata(), keep_blank_values=True))
    pairs["hash"] = "0" * 64
    assert verify_telegram_webapp(urlencode(pairs)) is None


def test_a_day_old_mini_app_launch_is_replayable_no_more():
    assert verify_telegram_webapp(_fresh_initdata(auth_date=str(int(time.time()) - 86_401))) is None
    assert verify_telegram_webapp(_fresh_initdata(auth_date=str(int(time.time()) - 60))) is not None


def test_the_widget_and_mini_app_schemes_do_not_cross_verify():
    # A widget-signed payload must NOT pass Mini App verification and vice versa —
    # the two use different secret derivations, and mixing them would be a hole.
    widget = _fresh()
    assert verify_telegram_webapp(urlencode(widget)) is None


def test_session_cookie_round_trips():
    assert read_session(issue_session(77)) == 77


def test_session_cookie_cannot_be_edited_by_its_bearer():
    cookie = issue_session(77)
    body, _, sig = cookie.partition(".")
    forged = issue_session(78).partition(".")[0] + "." + sig  # swap the identity, keep the signature
    assert read_session(forged) is None
    assert read_session(body) is None  # signature stripped
    assert read_session(None) is None
    assert read_session("garbage") is None
