"""The Telegram signature is the only thing between a visitor and someone else's
account: /api/auth/telegram accepts whatever JSON is POSTed to it."""

import hashlib
import hmac
import time

from src.api.auth import issue_session, read_session, verify_telegram_login
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
