import hashlib
import hmac
from datetime import UTC, datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select

from src.core.config import settings
from src.core.database import async_session_maker
from src.models import Server


def normalize_fingerprint(value: str) -> str:
    return "".join(character for character in value.lower() if character in "0123456789abcdef")


def hash_node_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid node credentials",
        headers={"Cache-Control": "no-store"},
    )


async def authenticate_node(request: Request) -> Server:
    configured_proxy_secret = settings.effective_node_control_proxy_secret
    if configured_proxy_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Node control is not configured",
            headers={"Cache-Control": "no-store"},
        )

    proxy_secret = request.headers.get("X-Aegis-Proxy-Secret", "")
    fingerprint = normalize_fingerprint(request.headers.get("X-Aegis-Node-Fingerprint", ""))
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if not (
        hmac.compare_digest(
            proxy_secret,
            configured_proxy_secret.get_secret_value(),
        )
        and fingerprint
        and scheme.lower() == "bearer"
        and token
    ):
        raise _unauthorized()

    async with async_session_maker() as session:
        candidates = (
            (
                await session.execute(
                    select(Server).where(
                        or_(
                            Server.control_cert_fingerprint == fingerprint,
                            Server.control_previous_cert_fingerprint == fingerprint,
                        ),
                        Server.control_mode.in_(("observe", "pull")),
                    )
                )
            )
            .scalars()
            .all()
        )

    token_hash = hash_node_token(token)
    now = datetime.now(UTC).replace(tzinfo=None)
    authenticated: list[Server] = []
    for candidate in candidates:
        current_pair = bool(
            candidate.control_cert_fingerprint
            and candidate.control_token_hash
            and hmac.compare_digest(
                candidate.control_cert_fingerprint,
                fingerprint,
            )
            and hmac.compare_digest(candidate.control_token_hash, token_hash)
        )
        previous_pair = bool(
            candidate.control_previous_cert_fingerprint
            and candidate.control_previous_token_hash
            and candidate.control_previous_credential_expires_at
            and candidate.control_previous_credential_expires_at >= now
            and hmac.compare_digest(
                candidate.control_previous_cert_fingerprint,
                fingerprint,
            )
            and hmac.compare_digest(
                candidate.control_previous_token_hash,
                token_hash,
            )
        )
        if current_pair or previous_pair:
            authenticated.append(candidate)

    if len(authenticated) != 1:
        raise _unauthorized()
    return authenticated[0]
