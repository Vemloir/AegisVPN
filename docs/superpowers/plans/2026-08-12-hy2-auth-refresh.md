# Hysteria2 Auth Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the Hysteria2 in-memory credential map synchronized when pull/apply reconciliation adds a device without removing any existing client.

**Architecture:** The authoritative reconciler already constructs and persists the complete Xray client configuration. Immediately after that write, pass the same configuration object to `hysteria.refresh_from_config`, matching the existing push handlers and preserving the current live-Xray and revocation ordering.

**Tech Stack:** Python 3.14, FastAPI agent, pytest, Ruff, Docker Compose deployment.

## Global Constraints

- Work directly on `main`, as explicitly authorized by the repository owner.
- Do not query or mutate production user records.
- Do not change subscription output, DNS policy, protocol selection, certificates, UUIDs, or Hysteria server configuration.
- Preserve unrelated untracked `AGENTS.md` and `landing/` content.
- Recreate only the agent container during rollout; do not restart Xray or Hysteria.

---

### Task 1: Refresh Hysteria Credentials for Add-Only Reconciliation

**Files:**
- Modify: `agent/tests/test_reconcile.py`
- Modify: `agent/app/reconcile.py`

**Interfaces:**
- Consumes: `hysteria.refresh_from_config(config: dict) -> None`
- Produces: `reconcile_snapshot(...)` refreshes HY2 auth state after every persisted configuration change.

- [ ] **Step 1: Write the failing add-only regression test**

Create an initial Xray config containing only the existing `keep` client, then apply `_snapshot()`, which adds `new-device`. Capture the real configuration passed to the HY2 refresh boundary and assert that both UUIDs are present, no kick occurs, and a repeated identical snapshot performs no second refresh.

```python
async def test_add_only_refreshes_hysteria_auth_without_restart(monkeypatch, tmp_path):
    config_path, additions, removals, events = await _patch_runtime(monkeypatch, tmp_path)
    config = json.loads(config_path.read_text())
    for inbound in config["inbounds"]:
        if inbound.get("protocol") == "vless":
            inbound["settings"]["clients"] = [
                client for client in inbound["settings"]["clients"]
                if client["id"] == "keep"
            ]
    config_path.write_text(json.dumps(config))

    result = await reconcile.reconcile_snapshot(_snapshot(), observe=False)

    assert result.added == 2
    assert result.removed == 0
    refreshes = [payload for kind, payload in events if kind == "refresh"]
    assert len(refreshes) == 1
    assert {
        client["id"]
        for inbound in refreshes[0]["inbounds"]
        if inbound.get("protocol") == "vless"
        for client in inbound["settings"]["clients"]
    } == {"keep", "new-device"}
    assert all(kind != "kick" for kind, _ in events)

    events.clear()
    repeated = await reconcile.reconcile_snapshot(_snapshot(), observe=False)
    assert repeated.changed is False
    assert events == []
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd agent
UV_CACHE_DIR=/tmp/aegis-uv-cache uv run pytest -q tests/test_reconcile.py::test_add_only_refreshes_hysteria_auth_without_restart
```

Expected: FAIL because the add-only path produces no `refresh` event.

- [ ] **Step 3: Implement the minimal synchronization fix**

In `reconcile_snapshot`, refresh HY2 auth immediately after the durable config write:

```python
if config_changed:
    await save_xray_config(config)
    hysteria.refresh_from_config(config)
```

- [ ] **Step 4: Run focused and reconciliation tests**

Run:

```bash
cd agent
UV_CACHE_DIR=/tmp/aegis-uv-cache uv run pytest -q tests/test_reconcile.py
```

Expected: all reconciliation tests pass.

- [ ] **Step 5: Run full verification**

Run:

```bash
cd agent
UV_CACHE_DIR=/tmp/aegis-uv-cache uv run pytest -q
UV_CACHE_DIR=/tmp/aegis-uv-cache uv run ruff check app tests
```

Expected: zero failures and zero lint errors.

- [ ] **Step 6: Commit and push**

```bash
git add agent/app/reconcile.py agent/tests/test_reconcile.py docs/superpowers/plans/2026-08-12-hy2-auth-refresh.md
git commit -m "fix(hy2): refresh auth after pull reconciliation"
git push origin main
```

### Task 2: Canary and Fleet Rollout

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: `deploy.vps.update.update_agent(c, host)`
- Produces: updated `aegis-vpn` agent containers on VPN nodes while preserving `aegis-xray` and `aegis-hysteria` uptime.

- [ ] **Step 1: Deploy Germany and Switzerland canaries**

Use verified SSH host keys and the existing deployment helper. Switzerland uses the `ubuntu` account; elevate only the normal deployment operations required by its current installation layout.

- [ ] **Step 2: Verify canary runtime state**

For each canary, verify agent health, HY2 container uptime, UDP/443 listener, certificate dates/SNI, and unchanged Xray/Hysteria container start timestamps.

- [ ] **Step 3: Verify auth-map behavior without user data**

Exercise the normal node reconciliation/auth boundary with a generated synthetic UUID, confirm immediate HY2 authorization state, then remove the synthetic identity. Do not inspect user UUIDs or subscription records.

- [ ] **Step 4: Deploy remaining nodes**

After both canaries pass, update USA, Hong Kong, Finland, and the Poland control/data node where applicable using the same agent-only operation.

- [ ] **Step 5: Verify fleet health**

Confirm every updated node reports healthy agent state and that Xray/Hysteria data-plane containers were not recreated.
