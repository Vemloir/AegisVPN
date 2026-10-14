"""Preview locations: a node on trial is listed only to the preview testers."""

import base64

from httpx import ASGITransport, AsyncClient

from src.api.main import app
from src.core.config import Settings, settings
from src.core.database import async_session_maker
from src.services import SubscriptionService
from tests.test_cdn import _cdn_node, _json_configs, _seed

TESTER = 940001  # the subscriber _seed creates


async def _seed_with_preview_node(monkeypatch) -> str:
    # Two locations: "Finland | Helsinki" for everyone, "Germany | Frankfurt" on trial.
    return await _seed(monkeypatch, cdn_server=_cdn_node(preview_only=True), endpoints=[])


async def test_everyone_else_does_not_see_a_preview_location(monkeypatch):
    monkeypatch.setattr(settings, "preview_tg_ids", [123])
    token = await _seed_with_preview_node(monkeypatch)
    remarks = [cfg["remarks"] for cfg in await _json_configs(token)]
    assert not [r for r in remarks if "Germany" in r]
    async with async_session_maker() as session:
        body = await SubscriptionService.get_subscription_vless_links(session, token)
    links = base64.b64decode(body).decode().splitlines()
    assert len(links) == 1 and "@203.0.113.30:" not in links[0]


async def test_a_tester_sees_it_marked_and_never_in_auto_select(monkeypatch):
    monkeypatch.setattr(settings, "preview_tg_ids", [TESTER])
    token = await _seed_with_preview_node(monkeypatch)
    configs = await _json_configs(token)
    remarks = [cfg["remarks"] for cfg in configs]
    assert any(r.endswith("Germany | Frankfurt | Preview") for r in remarks)
    # Auto-select would need two ordinary locations; the trial one never counts.
    assert not any("Автовыбор" in r for r in remarks)
    for cfg in configs:
        addresses = {
            server["address"]
            for outbound in cfg["outbounds"]
            for server in outbound.get("settings", {}).get("vnext", [])
        }
        assert not ({"203.0.113.30", "203.0.113.40"} <= addresses)


async def test_the_public_site_never_lists_a_preview_location(monkeypatch):
    monkeypatch.setattr(settings, "preview_tg_ids", [TESTER])
    await _seed_with_preview_node(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        names = [location["name"] for location in (await client.get("/api/locations")).json()]
    assert names == ["Finland | Helsinki"]


def test_the_older_setting_name_is_still_read(monkeypatch):
    monkeypatch.delenv("PREVIEW_TG_IDS", raising=False)
    monkeypatch.setenv("CDN_WL_PREVIEW_TG_IDS", "[6367722457]")
    assert Settings().preview_tg_ids == [6367722457]
    monkeypatch.setenv("PREVIEW_TG_IDS", "[1]")
    assert Settings().preview_tg_ids == [1]
