"""Thin async client for the Platega payment API (СБП / RUB acquiring).

Docs: https://docs.platega.io — base https://app.platega.io, auth via the
X-MerchantId / X-Secret headers on every request. Only the two calls we need:
create an СБП transaction and read its status (used both as a manual
"я оплатил" check and as a fallback for the async callback).
"""

import hmac
from typing import Any

import aiohttp

from src.core.config import settings
from src.core.logger import logger

# Platega payment-method codes (see the create-transaction doc).
PAYMENT_METHOD_SBP = 2

_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5)


class PlategaError(Exception):
    """A Platega API call failed (non-200 or transport error)."""


def _headers() -> dict[str, str]:
    return {
        "X-MerchantId": settings.platega_merchant_id or "",
        "X-Secret": settings.platega_secret.get_secret_value() if settings.platega_secret else "",
        "Content-Type": "application/json",
    }


async def create_sbp_transaction(
    *,
    amount_rub: int,
    description: str,
    payload: str,
    user_id: int,
    username: str | None,
    return_url: str,
    failed_url: str,
) -> dict[str, Any]:
    """Create an СБП QR transaction. Returns the parsed response
    (``transactionId``, ``redirect`` payment URL, ``status``, ...)."""
    body = {
        "paymentMethod": PAYMENT_METHOD_SBP,
        "paymentDetails": {"amount": amount_rub, "currency": "RUB"},
        "description": description,
        "return": return_url,
        "failedUrl": failed_url,
        "payload": payload,
        # userId enables Platega's fraud protection for our merchant category;
        # omitting it can get the account suspended, per the docs.
        "metadata": {"userId": str(user_id), "userName": username or ""},
    }
    url = f"{settings.platega_base_url.rstrip('/')}/transaction/process"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, json=body, headers=_headers()) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise PlategaError(f"create_sbp_transaction {resp.status}: {text[:300]}")
                return await resp.json()
    except aiohttp.ClientError as exc:
        raise PlategaError(f"create_sbp_transaction transport error: {exc}") from exc


async def get_transaction_status(transaction_id: str) -> dict[str, Any]:
    """Read a transaction. Returns the parsed body incl. ``status``
    (PENDING / CONFIRMED / CANCELED / CHARGEBACKED)."""
    url = f"{settings.platega_base_url.rstrip('/')}/transaction/{transaction_id}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, headers=_headers()) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise PlategaError(f"get_transaction_status {resp.status}: {text[:300]}")
                return await resp.json()
    except aiohttp.ClientError as exc:
        raise PlategaError(f"get_transaction_status transport error: {exc}") from exc


def verify_callback_headers(merchant_id: str | None, secret: str | None) -> bool:
    """Platega authenticates its callback by sending our own X-MerchantId /
    X-Secret back to us (no HMAC). Verify they match our configured creds."""
    if not settings.platega_enabled:
        return False
    expected_secret = settings.platega_secret.get_secret_value() if settings.platega_secret else ""
    expected_mid = settings.platega_merchant_id or ""
    # Constant-time compare so a forged callback can't probe the secret by timing.
    ok = hmac.compare_digest((merchant_id or "").encode(), expected_mid.encode()) and hmac.compare_digest(
        (secret or "").encode(), expected_secret.encode()
    )
    if not ok:
        logger.warning("Platega callback rejected: merchant/secret header mismatch")
    return ok
