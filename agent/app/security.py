"""Bearer-token authentication for the agent API."""

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    if credentials.credentials != settings.agent_token:
        raise HTTPException(status_code=403, detail="Invalid token")
    return credentials.credentials
