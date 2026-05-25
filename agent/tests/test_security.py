import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.config import settings
from app.security import verify_token


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_verify_token_accepts_matching_token():
    assert verify_token(_creds(settings.agent_token)) == settings.agent_token


def test_verify_token_rejects_wrong_token():
    with pytest.raises(HTTPException) as exc:
        verify_token(_creds("wrong"))
    assert exc.value.status_code == 403
