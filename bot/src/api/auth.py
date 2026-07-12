"""Telegram Login Widget verification and the site's session cookie.

Two secrets never leave the server: the bot token (used to derive the key that
signs Telegram's payload) and the session key (used to sign our own cookie).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from src.core.config import settings

# Telegram considers a login payload fresh for a day; anything older is a replay
# of a captured callback URL.
AUTH_MAX_AGE_SECONDS = 86_400

SESSION_COOKIE = "aegis_session"
SESSION_MAX_AGE_SECONDS = 30 * 86_400


def _session_key() -> bytes:
    """Key for our own cookie signature.

    Derived from the bot token rather than added as a separate deployment
    secret, but domain-separated so a leak of one signature cannot be replayed
    against Telegram's scheme (which keys on sha256(token) directly).
    """
    token = settings.bot_token.get_secret_value().encode()
    return hashlib.sha256(b"aegis-web-session\x00" + token).digest()


def verify_telegram_login(payload: dict) -> int | None:
    """Return the Telegram user id if the payload is authentic and fresh.

    The widget signs every field except ``hash`` with
    HMAC-SHA256(data_check_string, SHA256(bot_token)). Anyone can POST whatever
    they like to /api/auth/telegram, so this signature is the ONLY thing standing
    between a visitor and impersonating an arbitrary account.
    """
    received = payload.get("hash")
    if not isinstance(received, str) or not received:
        return None

    fields = {k: v for k, v in payload.items() if k != "hash" and v is not None}
    if "id" not in fields or "auth_date" not in fields:
        return None

    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hashlib.sha256(settings.bot_token.get_secret_value().encode()).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received):
        return None

    try:
        auth_date = int(fields["auth_date"])
        tg_id = int(fields["id"])
    except (TypeError, ValueError):
        return None

    if time.time() - auth_date > AUTH_MAX_AGE_SECONDS:
        return None

    return tg_id


def issue_session(user_id: int) -> str:
    """Sign ``user_id|expiry`` so the cookie cannot be edited by its bearer."""
    expires = int(time.time()) + SESSION_MAX_AGE_SECONDS
    body = f"{user_id}:{expires}".encode()
    sig = hmac.new(_session_key(), body, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(body).decode()}.{base64.urlsafe_b64encode(sig).decode()}"


def read_session(cookie: str | None) -> int | None:
    """Return the signed-in user id, or None for a missing/forged/expired cookie."""
    if not cookie or "." not in cookie:
        return None
    raw_body, _, raw_sig = cookie.partition(".")
    try:
        body = base64.urlsafe_b64decode(raw_body.encode())
        sig = base64.urlsafe_b64decode(raw_sig.encode())
    except Exception:
        return None

    expected = hmac.new(_session_key(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        return None

    try:
        user_id_s, _, expires_s = body.decode().partition(":")
        if int(expires_s) < time.time():
            return None
        return int(user_id_s)
    except (TypeError, ValueError):
        return None
