# TCP+Vision Default Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make TCP+REALITY with `xtls-rprx-vision` the default VLESS transport while retaining explicit XHTTP selection and capability-aware fallback.

**Architecture:** `SubscriptionService` will resolve the effective default from each server's capabilities: TCP when `tcp_port` exists, otherwise XHTTP. Preference storage, subscription normalization, and the location UI will consume the same resolver so “no row”, displayed selection, and emitted client JSON cannot disagree.

**Tech Stack:** Python 3.13, SQLAlchemy async ORM, aiohttp subscription service, pytest/pytest-asyncio.

## Global Constraints

- Do not change subscription URLs, UUIDs, Reality keys, access assignments, or server ports.
- Keep XHTTP selectable per location.
- Do not restart Xray data-plane containers; deploy only the bot after verification.
- Existing missing preference rows intentionally migrate to TCP on the next subscription refresh.

---

### Task 1: Capability-aware preference semantics

**Files:**
- Modify: `bot/tests/test_transport_prefs.py`
- Modify: `bot/src/services/subscription_service.py`
- Modify: `bot/src/models/transport_pref.py`

**Interfaces:**
- Consumes: `Server.tcp_port`, `ServerTransportPref(protocol, transport)`.
- Produces: `SubscriptionService.default_transport_for(server) -> str`, `get_transport_pref(...) -> tuple[str, str]`, `_transport_prefs_for_user(...) -> dict[int, str]`.

- [ ] **Step 1: Write failing preference tests**

Update the tests to require:

```python
# TCP-capable server, no row
assert await SubscriptionService.get_transport_pref(session, user_id, server_id) == ("vless", "tcp")
assert await SubscriptionService._transport_prefs_for_user(session, user_id, [server_id]) == {server_id: "tcp"}

# XHTTP-only server, no row
assert await SubscriptionService.get_transport_pref(session, user_id, server_id) == ("vless", "xhttp")

# Explicit XHTTP on a TCP-capable server is stored; selecting TCP deletes the row.
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `cd bot && uv run pytest -q tests/test_transport_prefs.py`

Expected: failures showing the current default is XHTTP and TCP is stored as a non-default row.

- [ ] **Step 3: Implement the capability-aware resolver and storage rules**

In `SubscriptionService`:

```python
DEFAULT_TRANSPORT = TRANSPORT_TCP

@staticmethod
def default_transport_for(server: Server) -> str:
    return SubscriptionService.TRANSPORT_TCP if server.tcp_port else SubscriptionService.TRANSPORT_XHTTP
```

Use this resolver when no row exists, when validating a stale preference, and when deciding whether `set_transport_pref` should delete or persist a row. Return a resolved entry for every existing `server_id` from `_transport_prefs_for_user`, including missing rows, so subscription generation receives an explicit concrete transport.

Change the ORM default to `transport="tcp"`.

- [ ] **Step 4: Run preference tests and verify GREEN**

Run: `cd bot && uv run pytest -q tests/test_transport_prefs.py`

Expected: all tests pass.

---

### Task 2: Client-visible TCP+Vision output and UI consistency

**Files:**
- Modify: `bot/tests/test_subscription_service.py`
- Modify: `bot/tests/test_location_keyboards.py`
- Modify: `bot/src/services/subscription_service.py`
- Modify: `bot/src/handlers/user/locations.py`

**Interfaces:**
- Consumes: `default_transport_for(server)` and normalized per-server preference map from Task 1.
- Produces: TCP-capable default links using `tcp_port`, `network=tcp`, and `flow=xtls-rprx-vision`; XHTTP-only default links remain XHTTP.

- [ ] **Step 1: Write failing client-output and UI tests**

Require a TCP-capable server with no explicit preference to produce:

```python
assert "type=tcp" in link
assert "@203.0.113.10:2053" in link
assert "flow=xtls-rprx-vision" in link
```

Require `available_transports` to list `tcp` before `xhttp` on capable nodes and only `xhttp` on XHTTP-only nodes. Require the location settings keyboard to display TCP as the selected missing-row default.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `cd bot && uv run pytest -q tests/test_subscription_service.py tests/test_location_keyboards.py`

Expected: failures on the former XHTTP default and ordering.

- [ ] **Step 3: Implement client-visible semantics**

Make `available_transports(server)` return `["tcp", "xhttp"]` when TCP-capable and `["xhttp"]` otherwise. Ensure `normalize_vless_uri(..., transport=None)` resolves through `default_transport_for(server)` rather than inheriting XHTTP from the raw safe template. Update handler comments and invalid-choice fallback to use the per-server default.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `cd bot && uv run pytest -q tests/test_subscription_service.py tests/test_location_keyboards.py`

Expected: all tests pass.

---

### Task 3: Regression verification and bot-only rollout

**Files:**
- Verify: `bot/`
- Deploy: `deploy/vps/update.py`

**Interfaces:**
- Consumes: completed bot implementation.
- Produces: updated production subscription responses after clients refresh.

- [ ] **Step 1: Run complete bot verification**

Run: `cd bot && uv run pytest -q`

Run: `cd bot && uv run ruff check src tests`

Expected: zero failures and zero lint errors.

- [ ] **Step 2: Refresh the knowledge graph**

Run: `graphify update .`

Expected: graph updated; if the local `graphify` executable is unavailable, record that limitation without blocking verified code.

- [ ] **Step 3: Commit and push main**

```bash
git add bot/src bot/tests docs/superpowers/plans/2026-08-10-tcp-vision-default.md
git commit -m "fix(subscription): default VLESS locations to TCP Vision"
git push origin main
```

- [ ] **Step 4: Deploy only the control-plane bot**

Use the existing host-key-verified deployment path in `deploy/vps/update.py` to rebuild/recreate `aegis-bot` and `aegis-siteapi`. Do not invoke node/Xray rollout options.

- [ ] **Step 5: Verify production without reading user records**

Check bot/site health, container restart state, and a synthetic/local subscription fixture or public health endpoint. Do not query user/device/subscription rows. Confirm that no Xray container was restarted by this rollout.
