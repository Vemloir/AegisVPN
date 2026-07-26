"""Private account data must not leak through the public site API."""

import pytest
from fastapi.testclient import TestClient

from src.api.auth import issue_session
from src.api.main import app
from src.core.database import async_session_maker, engine
from src.core.terms import TERMS_VERSION
from src.models.base import Base
from src.models.user import User


@pytest.fixture(autouse=True)
async def _schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed_user() -> int:
    async with async_session_maker() as session:
        user = User(
            tg_id=771001,
            username="private_user",
            first_name="Private",
            avatar_data=b"private-avatar",
            avatar_mime="image/webp",
        )
        session.add(user)
        await session.commit()
        return user.id


def _client(user_id: int | None = None) -> TestClient:
    client = TestClient(app)
    if user_id is not None:
        client.cookies.set("aegis_session", issue_session(user_id))
    return client


async def test_me_is_private_and_points_only_to_the_current_users_avatar():
    user_id = await _seed_user()

    response = _client(user_id).get("/api/me")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["avatar_url"].startswith("/api/avatar/me?v=")
    assert response.json()["avatar_url"].split("?", 1)[0] == "/api/avatar/me"


async def test_avatar_requires_a_session_and_only_serves_its_owner():
    user_id = await _seed_user()

    anonymous = _client().get("/api/avatar/me")
    assert anonymous.status_code == 401
    assert anonymous.headers["cache-control"] == "no-store"

    own = _client(user_id).get("/api/avatar/me")
    assert own.status_code == 200
    assert own.content == b"private-avatar"
    assert own.headers["content-type"] == "image/webp"
    assert own.headers["cache-control"].startswith("private,")

    assert _client().get(f"/api/avatar/{user_id}").status_code == 404


async def test_malformed_auth_json_is_a_client_error_not_a_server_error():
    client = _client()
    for path in ("/api/auth/telegram", "/api/auth/tma"):
        response = client.post(
            path,
            content=b"{definitely-not-json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"error": "bad_request"}


async def test_anonymous_me_response_is_not_cacheable():
    response = _client().get("/api/me")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_current_privacy_policy_discloses_cached_telegram_profile_data():
    response = _client().get("/api/legal/privacy?lang=ru")
    text = response.json()["text"].lower()

    assert response.json()["version"] == "2026-07-27"
    assert TERMS_VERSION == "2026-07-27"
    assert "имя и фамили" in text
    assert "аватар" in text
    assert "фотограф" in text
