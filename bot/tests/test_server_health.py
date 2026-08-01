from datetime import UTC, datetime, timedelta

from src.models import NodeTelemetry, Server
from src.scheduler import tasks


def _pull_server(now: datetime) -> Server:
    return Server(
        id=9,
        name="Pull node",
        flag="P",
        host="203.0.113.9",
        port=443,
        public_key="pk",
        short_id="sid",
        agent_url="http://private.invalid:8444",
        agent_token="not-used",
        control_mode="pull",
        desired_generation=7,
        applied_generation=7,
        control_last_seen_at=now - timedelta(seconds=20),
        control_last_reconciled_at=now - timedelta(seconds=30),
        control_last_error=None,
        is_active=True,
    )


async def test_pull_health_uses_outbound_control_state_not_public_agent(monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    server = _pull_server(now)
    telemetry = NodeTelemetry(
        server_id=server.id,
        sequence=5,
        payload={"online_emails": ["user_1_sub_1"]},
        received_at=now - timedelta(seconds=10),
    )

    class ForbiddenAgentClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("pull health must not call the public Agent API")

    monkeypatch.setattr(tasks, "AgentClient", ForbiddenAgentClient)
    monkeypatch.setattr(tasks, "_probe_xray_port", lambda *_args, **_kwargs: _true())

    ok, reason, clients = await tasks._check_one(server, telemetry=telemetry, now=now)

    assert ok is True
    assert reason == ""
    assert clients == 1


async def test_pull_health_rejects_stale_or_unapplied_control_state(monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    server = _pull_server(now)
    server.applied_generation = 6
    server.control_last_seen_at = now - timedelta(minutes=10)
    monkeypatch.setattr(tasks, "_probe_xray_port", lambda *_args, **_kwargs: _true())

    ok, reason, _ = await tasks._check_one(server, telemetry=None, now=now)

    assert ok is False
    assert "stale" in reason
    assert "generation" in reason


async def _true():
    return True
