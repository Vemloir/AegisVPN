import hashlib
import hmac

from fastapi import HTTPException, Request, status
from sqlalchemy import select

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
    )


async def authenticate_node(request: Request) -> Server:
    configured_proxy_secret = settings.effective_node_control_proxy_secret
    if configured_proxy_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Node control is not configured",
        )

    proxy_secret = request.headers.get("X-Aegis-Proxy-Secret", "")
    fingerprint = normalize_fingerprint(
        request.headers.get("X-Aegis-Node-Fingerprint", "")
    )
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
        server = (
            await session.execute(
                select(Server).where(
                    Server.control_cert_fingerprint == fingerprint,
                    Server.control_mode.in_(("observe", "pull")),
                )
            )
        ).scalar_one_or_none()

    if (
        server is None
        or not server.control_token_hash
        or not hmac.compare_digest(
            hash_node_token(token),
            server.control_token_hash,
        )
    ):
        raise _unauthorized()
    return server
