"""Per-location transport preferences: storage, default-means-no-row, reset,
and the user->server resolution map that feeds the subscription builder.

The whole suite shares one SQLite file (see conftest); each test rebuilds the
schema so test_migrations leaving minimal tables behind does not interfere.
"""

from sqlalchemy import select

from src.core.database import async_session_maker, engine
from src.models import Server, ServerTransportPref, User
from src.models.base import Base
from src.services import SubscriptionService


async def _fresh_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def _seed(alt: bool = True) -> tuple[int, int]:
    """Create one user + one Greece-like server. Returns (user_id, server_id)."""
    await _fresh_db()
    async with async_session_maker() as session:
        user = User(tg_id=910001)
        session.add(user)
        await session.flush()
        server = Server(
            name="Greece",
            flag="GR",
            host="45.142.31.13",
            port=443,
            public_key="PBK",
            short_id="SID",
            agent_url="http://x",
            agent_token="t",
            tcp_port=2053 if alt else None,
        )
        session.add(server)
        await session.flush()
        await session.commit()
        return user.id, server.id


async def test_default_means_no_row():
    user_id, server_id = await _seed()
    async with async_session_maker() as session:
        # No row yet -> default (vless, xhttp).
        protocol, transport = await SubscriptionService.get_transport_pref(session, user_id, server_id)
        assert (protocol, transport) == ("vless", "xhttp")
        # The build-path map is empty (every server stays on its default).
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        assert mapping == {}


async def test_set_tcp_pref_round_trips_into_build_map():
    user_id, server_id = await _seed()
    async with async_session_maker() as session:
        await SubscriptionService.set_transport_pref(session, user_id, server_id, "vless", "tcp")
    async with async_session_maker() as session:
        protocol, transport = await SubscriptionService.get_transport_pref(session, user_id, server_id)
        assert (protocol, transport) == ("vless", "tcp")
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        assert mapping == {server_id: "tcp"}


async def test_selecting_default_deletes_the_row():
    user_id, server_id = await _seed()
    async with async_session_maker() as session:
        await SubscriptionService.set_transport_pref(session, user_id, server_id, "vless", "tcp")
    async with async_session_maker() as session:
        # Re-selecting the plain default collapses back to "no row".
        await SubscriptionService.set_transport_pref(session, user_id, server_id, "vless", "xhttp")
    async with async_session_maker() as session:
        rows = (await session.execute(select(ServerTransportPref))).scalars().all()
        assert rows == []


async def test_reset_clears_a_locations_pref():
    user_id, server_id = await _seed()
    async with async_session_maker() as session:
        await SubscriptionService.set_transport_pref(session, user_id, server_id, "vless", "tcp")
    async with async_session_maker() as session:
        await SubscriptionService.reset_transport_pref(session, user_id, server_id)
    async with async_session_maker() as session:
        protocol, transport = await SubscriptionService.get_transport_pref(session, user_id, server_id)
        assert (protocol, transport) == ("vless", "xhttp")
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        assert mapping == {}


async def test_stale_tcp_pref_on_xhttp_only_server_is_omitted():
    # Server has no alt transports, but a tcp pref row exists (capability lost).
    user_id, server_id = await _seed(alt=False)
    async with async_session_maker() as session:
        # Set the pref directly (set_transport_pref would also store it).
        session.add(
            ServerTransportPref(user_id=user_id, server_id=server_id, protocol="vless", transport="tcp")
        )
        await session.commit()
    async with async_session_maker() as session:
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        # Resolves to xhttp default -> omitted from the override map (byte-identical).
        assert mapping == {}
