import pytest
from fastapi import HTTPException

from app import main


async def test_startup_retries_pending_revocations_after_auth_refresh(monkeypatch):
    events: list[str] = []

    async def refresh():
        events.append("refresh")

    async def retry_pending():
        events.append("retry")
        return True

    def discard_task(coroutine, *args, **kwargs):
        coroutine.close()
        return None

    monkeypatch.setattr(main.hysteria, "refresh", refresh)
    monkeypatch.setattr(
        main,
        "retry_pending_revocations",
        retry_pending,
        raising=False,
    )
    monkeypatch.setattr(main.asyncio, "create_task", discard_task)
    monkeypatch.setattr(main.settings, "control_mode", "off")
    monkeypatch.setattr(main.settings, "hy2_enabled", False)

    await main._start_background_tasks()

    assert events == ["refresh", "retry"]


async def test_health_is_not_ready_when_control_supervisor_died(monkeypatch):
    class DeadTask:
        def done(self):
            return True

    async def config():
        return {"inbounds": []}

    monkeypatch.setattr(main.settings, "control_mode", "apply")
    monkeypatch.setattr(main, "_control_task", DeadTask())
    monkeypatch.setattr(main, "get_xray_config", config)

    with pytest.raises(HTTPException) as error:
        await main.health()

    assert error.value.status_code == 503
