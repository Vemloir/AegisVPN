import uuid

from src.control.state import build_desired_items, publish_snapshot
from src.core.database import async_session_maker, engine
from src.models import Base, CascadeRoute, CascadeRouteAck, CascadeRouteExit, Server
from src.services.cascade_service import advertisable_routes, current_route_digest


def _server(name: str, host: str, role: str) -> Server:
    return Server(
        name=name,
        flag="X",
        host=host,
        port=443,
        public_key=f"pk-{name}",
        short_id=f"sid-{name}",
        agent_url="http://127.0.0.1:8444",
        agent_token="legacy",
        control_mode="pull",
        control_capabilities={"features": ["cascade-v2"]},
        node_role=role,
        is_active=True,
    )


async def _seed_route():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        entry = _server("entry", "203.0.113.10", "entry")
        exit_one = _server("exit-one", "198.51.100.11", "exit")
        exit_two = _server("exit-two", "198.51.100.12", "exit")
        unrelated = _server("unrelated", "198.51.100.13", "both")
        session.add_all([entry, exit_one, exit_two, unrelated])
        await session.flush()
        route = CascadeRoute(
            label="Russia → Germany | Frankfurt",
            entry_server_id=entry.id,
            enabled=True,
            revision=3,
            health_policy={"strategy": "leastPing", "probe_interval": "10s"},
            transport_policy={"network": "xhttp"},
        )
        session.add(route)
        await session.flush()
        session.add_all(
            [
                CascadeRouteExit(
                    route_id=route.id,
                    exit_server_id=exit_one.id,
                    position=0,
                    service_uuid=str(uuid.uuid4()),
                    server_name="www.cloudflare.com",
                    xhttp_path="/cascade-a",
                    enabled=True,
                ),
                CascadeRouteExit(
                    route_id=route.id,
                    exit_server_id=exit_two.id,
                    position=1,
                    service_uuid=str(uuid.uuid4()),
                    server_name="www.microsoft.com",
                    xhttp_path="/cascade-b",
                    enabled=True,
                ),
            ]
        )
        await session.commit()
        return entry.id, exit_one.id, exit_two.id, unrelated.id, route.id


async def test_v2_snapshots_are_node_scoped_and_v1_ignores_cascade():
    entry_id, exit_one_id, exit_two_id, unrelated_id, route_id = await _seed_route()
    async with async_session_maker() as session:
        entry_items = await build_desired_items(session, entry_id)
        exit_one_items = await build_desired_items(session, exit_one_id)
        exit_two_items = await build_desired_items(session, exit_two_id)
        unrelated_items = await build_desired_items(session, unrelated_id)

        assert [item["kind"] for item in entry_items] == ["cascade_route"]
        assert entry_items[0]["route_id"] == route_id
        assert len(entry_items[0]["exits"]) == 2
        assert [item["kind"] for item in exit_one_items] == ["cascade_service"]
        assert [item["kind"] for item in exit_two_items] == ["cascade_service"]
        assert exit_one_items[0]["uuid"] != exit_two_items[0]["uuid"]
        assert unrelated_items == []

        entry = await session.get(Server, entry_id)
        entry.control_capabilities = {"features": []}
        await session.flush()
        assert await build_desired_items(session, entry_id) == []
        snapshot = await publish_snapshot(session, entry_id, page_size=100)
        assert snapshot.schema_version == 1


async def test_route_is_not_advertisable_until_entry_and_every_exit_ack_revision():
    entry_id, exit_one_id, exit_two_id, _, route_id = await _seed_route()
    async with async_session_maker() as session:
        assert await advertisable_routes(session, {entry_id}) == []
        for server_id in (entry_id, exit_one_id):
            session.add(
                CascadeRouteAck(
                    route_id=route_id,
                    server_id=server_id,
                    revision=3,
                    generation=7,
                    config_digest=await current_route_digest(session, route_id),
                )
            )
        await session.commit()
        assert await advertisable_routes(session, {entry_id}) == []

        session.add(
            CascadeRouteAck(
                route_id=route_id,
                server_id=exit_two_id,
                revision=3,
                generation=8,
                config_digest=await current_route_digest(session, route_id),
            )
        )
        await session.commit()
        routes = await advertisable_routes(session, {entry_id})
        assert [route.label for route in routes] == ["Russia → Germany | Frankfurt"]

        route_exit = await session.get(
            CascadeRouteExit,
            {"route_id": route_id, "exit_server_id": exit_two_id},
        )
        route_exit.xhttp_path = "/rotated-without-ack"
        await session.commit()
        assert await advertisable_routes(session, {entry_id}) == []
