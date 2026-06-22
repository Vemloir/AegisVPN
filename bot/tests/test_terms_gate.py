"""Legal-acceptance gate: acceptance state, version bumping, grandfathering,
the gate message structure, and the Markdown->Telegraph conversion for the ToS.
"""

from sqlalchemy import text

from src.core.database import async_session_maker
from src.core.migrations import run_migrations
from src.core.terms import TERMS_VERSION
from src.models import User
from src.services import UserService
from src.services.telegraph import _inline_children, _to_nodes


async def _reset_users():
    await run_migrations()
    async with async_session_maker() as session:
        await session.execute(text("DELETE FROM users"))
        await session.commit()


async def test_new_user_is_not_accepted_then_accepts():
    await _reset_users()
    assert await UserService.is_terms_accepted(900001) is False

    language, can_use_trial = await UserService.accept_terms(900001, "newuser", "ru")
    assert language == "ru"
    assert can_use_trial is True
    assert await UserService.is_terms_accepted(900001) is True

    async with async_session_maker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT accepted_terms_version, accepted_terms_at, privacy_accepted "
                    "FROM users WHERE tg_id = 900001"
                )
            )
        ).fetchone()
    assert row[0] == TERMS_VERSION
    assert row[1] is not None  # timestamp recorded
    assert bool(row[2]) is True  # legacy privacy flag kept in sync


async def test_version_bump_reprompts():
    await _reset_users()
    await UserService.accept_terms(900002, "u", "ru")
    assert await UserService.is_terms_accepted(900002) is True

    async with async_session_maker() as session:
        await session.execute(text("UPDATE users SET accepted_terms_version = 'OLD' WHERE tg_id = 900002"))
        await session.commit()
    assert await UserService.is_terms_accepted(900002) is False


async def test_legacy_privacy_user_is_grandfathered():
    await _reset_users()
    # A pre-gate user who accepted only the old privacy flow.
    async with async_session_maker() as session:
        session.add(User(tg_id=900003, username="legacy", privacy_accepted=True))
        session.add(User(tg_id=900004, username="never", privacy_accepted=False))
        await session.commit()
        # The migration's backfill statement, applied on upgrade:
        await session.execute(
            text(
                "UPDATE users SET accepted_terms_version = :v, accepted_terms_at = CURRENT_TIMESTAMP "
                "WHERE privacy_accepted = 1 AND accepted_terms_version IS NULL"
            ),
            {"v": TERMS_VERSION},
        )
        await session.commit()

    assert await UserService.is_terms_accepted(900003) is True  # grandfathered
    assert await UserService.is_terms_accepted(900004) is False  # never accepted -> gated


async def test_register_on_start_reports_terms_state():
    await _reset_users()
    language, can_use_trial, terms_ok, is_banned = await UserService.register_or_update_on_start(
        900005, "u", "ru", None
    )
    assert terms_ok is False  # brand-new user must be gated
    assert is_banned is False

    await UserService.accept_terms(900005)
    _, _, terms_ok, _ = await UserService.register_or_update_on_start(900005, "u", "ru", None)
    assert terms_ok is True


def test_inline_children_handles_markdown_and_html_bold():
    nodes = _inline_children("plain **bold** and <b>tag</b> end")
    strong = [n for n in nodes if isinstance(n, dict) and n["tag"] == "strong"]
    assert len(strong) == 2
    assert strong[0]["children"] == ["bold"]
    assert strong[1]["children"] == ["tag"]


def test_to_nodes_handles_markdown_structures():
    md = "## Heading\n\nA paragraph.\n\n- item one\n\n---\n\n> a quote"
    nodes = _to_nodes(md)
    tags = [n["tag"] for n in nodes]
    assert tags == ["h3", "p", "p", "hr", "blockquote"]
    # bullet rendered with our • prefix
    assert nodes[2]["children"][0].startswith("• ")
