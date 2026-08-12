"""Per-location transport preferences: capability-aware defaults, storage,
reset, and the user->server resolution map that feeds the subscription builder.

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
    """Create one user + one fully provisioned server. Returns (user_id, server_id)."""
    await _fresh_db()
    async with async_session_maker() as session:
        user = User(tg_id=910001)
        session.add(user)
        await session.flush()
        server = Server(
            name="Testland",
            flag="GR",
            host="203.0.113.10",
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
        # A TCP-capable location defaults to TCP+Vision without storing a row.
        protocol, transport = await SubscriptionService.get_transport_pref(session, user_id, server_id)
        assert (protocol, transport) == ("vless", "tcp")
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        assert mapping == {server_id: "tcp"}
        rows = (await session.execute(select(ServerTransportPref))).scalars().all()
        assert rows == []


async def test_xhttp_only_server_defaults_to_xhttp():
    user_id, server_id = await _seed(alt=False)
    async with async_session_maker() as session:
        protocol, transport = await SubscriptionService.get_transport_pref(session, user_id, server_id)
        assert (protocol, transport) == ("vless", "xhttp")
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        assert mapping == {server_id: "xhttp"}


async def test_explicit_xhttp_pref_round_trips_into_build_map():
    user_id, server_id = await _seed()
    async with async_session_maker() as session:
        await SubscriptionService.set_transport_pref(session, user_id, server_id, "vless", "xhttp")
    async with async_session_maker() as session:
        protocol, transport = await SubscriptionService.get_transport_pref(session, user_id, server_id)
        assert (protocol, transport) == ("vless", "xhttp")
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        assert mapping == {server_id: "xhttp"}
        rows = (await session.execute(select(ServerTransportPref))).scalars().all()
        assert len(rows) == 1


async def test_selecting_default_deletes_the_row():
    user_id, server_id = await _seed()
    async with async_session_maker() as session:
        await SubscriptionService.set_transport_pref(session, user_id, server_id, "vless", "xhttp")
    async with async_session_maker() as session:
        # Selecting the capability-aware TCP default collapses to "no row".
        await SubscriptionService.set_transport_pref(session, user_id, server_id, "vless", "tcp")
    async with async_session_maker() as session:
        rows = (await session.execute(select(ServerTransportPref))).scalars().all()
        assert rows == []


async def test_reset_clears_a_locations_pref():
    user_id, server_id = await _seed()
    async with async_session_maker() as session:
        await SubscriptionService.set_transport_pref(session, user_id, server_id, "vless", "xhttp")
    async with async_session_maker() as session:
        await SubscriptionService.reset_transport_pref(session, user_id, server_id)
    async with async_session_maker() as session:
        protocol, transport = await SubscriptionService.get_transport_pref(session, user_id, server_id)
        assert (protocol, transport) == ("vless", "tcp")
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        assert mapping == {server_id: "tcp"}


async def test_stale_tcp_pref_on_xhttp_only_server_falls_back_to_xhttp():
    # Server has no alt transports, but a tcp pref row exists (capability lost).
    user_id, server_id = await _seed(alt=False)
    async with async_session_maker() as session:
        # Set the pref directly (set_transport_pref would also store it).
        session.add(ServerTransportPref(user_id=user_id, server_id=server_id, protocol="vless", transport="tcp"))
        await session.commit()
    async with async_session_maker() as session:
        mapping = await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id])
        assert mapping == {server_id: "xhttp"}


async def test_stale_hy2_pref_is_displayed_as_effective_vless_tcp():
    """The settings UI must show what the subscription actually emits.

    Keep the stored row intact so an operator can re-enable Hy2 later, but when
    this node loses Hy2 capability expose the current VLESS/TCP fallback rather
    than a selected-but-disabled Hy2 value.
    """
    user_id, server_id = await _seed()
    async with async_session_maker() as session:
        session.add(
            ServerTransportPref(
                user_id=user_id,
                server_id=server_id,
                protocol="hy2",
                transport="xhttp",
            )
        )
        await session.commit()

    async with async_session_maker() as session:
        protocol, transport = await SubscriptionService.get_transport_pref(
            session, user_id, server_id
        )
        stored = await session.get(ServerTransportPref, (user_id, server_id))

    assert (protocol, transport) == ("vless", "tcp")
    assert stored is not None
    assert (stored.protocol, stored.transport) == ("hy2", "xhttp")
