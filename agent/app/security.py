"""Bearer-token authentication for the agent API."""

import hmac

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    if not hmac.compare_digest(credentials.credentials.encode(), settings.agent_token.encode()):
        raise HTTPException(status_code=403, detail="Invalid token")
    return credentials.credentials
