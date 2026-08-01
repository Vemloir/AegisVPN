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
                    "SELECT accepted_terms_version, accepted_terms_at, privacy_accepted FROM users WHERE tg_id = 900001"
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


async def test_grandfather_backfill_no_longer_silently_reaccepts():
    """The grandfather post_sql has been removed: adding the acceptance columns
    must NOT silently re-accept legacy privacy users. Explicit acceptance only."""
    await _reset_users()
    async with async_session_maker() as session:
        session.add(User(tg_id=900003, username="legacy", privacy_accepted=True))
        session.add(User(tg_id=900004, username="never", privacy_accepted=False))
        await session.commit()

    # No backfill is applied by the migration anymore, so neither is accepted.
    assert await UserService.is_terms_accepted(900003) is False
    assert await UserService.is_terms_accepted(900004) is False


async def test_force_reset_migration_nulls_acceptance():
    """The one-shot force-reacceptance migration NULLs every user's acceptance,
    even users the original grandfather migration already marked accepted."""
    await _reset_users()
    # Two users that look accepted (as prod is today after grandfathering).
    async with async_session_maker() as session:
        session.add(User(tg_id=900010, privacy_accepted=True, accepted_terms_version=TERMS_VERSION))
        session.add(User(tg_id=900011, privacy_accepted=True, accepted_terms_version=TERMS_VERSION))
        # Force the one-shot to be considered un-applied on this DB.
        await session.execute(text("DELETE FROM schema_meta"))
        await session.commit()

    assert await UserService.is_terms_accepted(900010) is True  # accepted before reset

    await run_migrations()  # applies the one-shot reset

    # Everyone is now NULLed -> re-gated on next interaction.
    assert await UserService.is_terms_accepted(900010) is False
    assert await UserService.is_terms_accepted(900011) is False
    async with async_session_maker() as session:
        rows = (
            await session.execute(text("SELECT accepted_terms_version, accepted_terms_at, privacy_accepted FROM users"))
        ).fetchall()
    for version, at, privacy in rows:
        assert version is None and at is None
        assert bool(privacy) is False

    # And it is one-shot: running migrations again does NOT re-run (idempotent),
    # so a user who just re-accepts is not wiped on the next restart.
    async with async_session_maker() as session:
        await session.execute(
            text("UPDATE users SET accepted_terms_version = :v WHERE tg_id = 900010"),
            {"v": TERMS_VERSION},
        )
        await session.commit()
    await run_migrations()
    assert await UserService.is_terms_accepted(900010) is True


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
