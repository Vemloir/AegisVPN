# Audit P1 Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Xray statistics, connection-limit enforcement, subprocess timeouts, Hysteria revocations, and the node control task fail safely under partial failures.

**Architecture:** Centralize Xray CLI execution and online-name normalization in `agent/app/xray.py`. Treat failed statistics as errors rather than empty state, persist Hysteria revocations before changing authorization, and expose control-loop liveness through agent readiness. This is the first independently deployable package from the 2026-08-01 audit; snapshot/DB performance and provisioning security remain separate packages.

**Tech Stack:** Python 3.14, asyncio, FastAPI, Pydantic, pytest/pytest-asyncio, Xray local CLI/API.

## Global Constraints

- Preserve the current `run_xray_api(args) -> tuple[int, str]` interface for existing callers.
- Never weaken the last-known-good connection-limit rule after incomplete statistics.
- Never ACK a desired generation while a revoked Hysteria session remains pending.
- Never log UUIDs, node credentials, subscription tokens, or generated profiles.
- Write every behavior test first and observe the expected failure before production edits.
- Do not modify the user-owned untracked `AGENTS.md` or `landing/` paths.

---

### Task 1: Shared Xray process runner and online-name normalization

**Files:**
- Modify: `agent/app/xray.py`
- Test: `agent/tests/test_xray.py`

**Interfaces:**
- Produces: `normalize_online_name(value: str) -> str | None`
- Produces: `_run_process(command: list[str], *, timeout: float, stderr_to_stdout: bool = False) -> tuple[int, bytes, bytes]`
- Preserves: `run_xray_api(args: list[str]) -> tuple[int, str]`

- [ ] **Step 1: Write failing parser tests**

Add literal pinned-output cases:

```python
def test_parse_online_users_normalizes_pinned_stat_names():
    raw = b'{"users":["user>>>user_1_sub_2_dev_3>>>online"]}'
    assert _parse_online_users(raw) == ["user_1_sub_2_dev_3"]

def test_parse_online_users_discards_malformed_stat_names():
    raw = b'{"users":["user>>>secret>>>traffic", "a>>>b>>>c>>>d"]}'
    assert _parse_online_users(raw) == []
```

- [ ] **Step 2: Run the parser tests and confirm they fail on raw stat names**

Run: `cd agent && .venv/bin/pytest tests/test_xray.py -q`

- [ ] **Step 3: Implement one strict normalizer and use it for dict, string-list, and record-list shapes**

Plain emails remain valid for compatibility; `user>>>EMAIL>>>online` returns only `EMAIL`; every other delimiter-containing shape returns `None`.

- [ ] **Step 4: Write a failing timeout/reap test**

Use a fake process whose first `communicate()` blocks, then returns after `kill()`. Assert the real helper calls `kill()` and awaits the second `communicate()` before returning a timeout result.

- [ ] **Step 5: Run the timeout test and confirm the current direct subprocess paths fail it**

Run: `cd agent && .venv/bin/pytest tests/test_xray.py -q`

- [ ] **Step 6: Implement `_run_process` and route `run_xray_api`, `get_online_emails`, and `query_traffic_stats` through it**

Timeout behavior must kill and reap. `get_online_emails` returns an empty list only for a successful empty response; process errors remain safely unavailable to telemetry. `query_traffic_stats` maps timeout to HTTP 504 and other process failures to HTTP 500.

- [ ] **Step 7: Run Xray tests**

Run: `cd agent && .venv/bin/pytest tests/test_xray.py -q`

### Task 2: Fail-safe connection limiter

**Files:**
- Modify: `agent/app/connlimit.py`
- Test: `agent/tests/test_connlimit.py`

**Interfaces:**
- Produces: `StatsQueryError(RuntimeError)`
- Changes: `online_users()` and `online_ips(email)` raise `StatsQueryError` for non-zero status, malformed JSON, or wrong payload shape.

- [ ] **Step 1: Write failing tests for a retained overflow rule after stats failure**

Exercise `enforce_conn_limit_once()` with `_prev_had_excess=True` and make either the all-users query or the second per-user query fail. Assert no `sib -reset` call occurs and `_prev_had_excess` remains `True`.

- [ ] **Step 2: Run limiter tests and confirm the reset is currently attempted or the failure is swallowed**

Run: `cd agent && .venv/bin/pytest tests/test_connlimit.py -q`

- [ ] **Step 3: Implement typed stats errors and collect the complete Xray sample before any kick/reset side effect**

Build all per-email IP maps first. Only after every query succeeds may the function compute Hysteria kicks and invoke `sib`. Let `StatsQueryError` propagate to `conn_limit_loop`, which logs the error type and retains last-known-good state.

- [ ] **Step 4: Run limiter and combined agent tests**

Run: `cd agent && .venv/bin/pytest tests/test_connlimit.py tests/test_xray.py -q`

### Task 3: Durable pending Hysteria revocations

**Files:**
- Modify: `agent/app/reconcile.py`
- Modify: `agent/app/main.py`
- Test: `agent/tests/test_reconcile.py`
- Test: `agent/tests/test_control_loop.py`

**Interfaces:**
- Produces: `load_pending_revocations() -> set[str]`
- Produces: `save_pending_revocations(values: set[str]) -> None`
- Produces: `retry_pending_revocations() -> bool`
- Stores: `/data/node-control/pending-revocations.json`, mode `0600`, via the existing atomic JSON writer.

- [ ] **Step 1: Write a failing same-snapshot retry test**

First `hysteria.kick()` returns `False`, second returns `True`. Assert the first reconcile raises without advancing applied state, the pending email exists on disk, the identical second snapshot kicks again, clears the journal, and advances applied state.

- [ ] **Step 2: Run the test and confirm the second reconcile currently skips the kick**

Run: `cd agent && .venv/bin/pytest tests/test_reconcile.py -q`

- [ ] **Step 3: Write a failing restart-retry test**

Seed the pending journal and an on-disk config without the revoked email. Assert `retry_pending_revocations()` refreshes Hysteria auth, kicks the pending email, and clears the journal only after success.

- [ ] **Step 4: Implement the journal ordering**

Persist newly removed emails before saving the desired config. Include existing pending entries in `changed`, refresh auth from the current config, retry all pending kicks, and clear the journal before saving `AppliedState`. If any kick or journal write fails, raise `ReconcileError` and do not ACK.

- [ ] **Step 5: Retry pending revocations during startup before starting control sync**

After `hysteria.refresh()`, call `retry_pending_revocations()`. A failed retry keeps the journal and is handled by the normal control loop; it must not reauthorize the removed identity.

- [ ] **Step 6: Run reconcile/control tests**

Run: `cd agent && .venv/bin/pytest tests/test_reconcile.py tests/test_control_loop.py -q`

### Task 4: Supervised control task and meaningful readiness

**Files:**
- Modify: `agent/app/control_loop.py`
- Modify: `agent/app/main.py`
- Test: `agent/tests/test_control_loop.py`
- Test: `agent/tests/test_main.py`

**Interfaces:**
- Produces: `ControlRuntimeStatus` containing `running`, `last_sync_at`, `last_telemetry_at`, and `last_error_type`.
- Produces: `control_readiness() -> dict[str, object]` with no secret-bearing exception text.

- [ ] **Step 1: Write failing tests for constructor failure and a dead task**

Assert invalid control credentials make startup/readiness non-ready instead of leaving shallow health `ok`. Assert an unexpectedly completed task is restarted with bounded backoff or marks readiness failed while the supervisor remains alive.

- [ ] **Step 2: Run focused tests and confirm current health ignores task state**

Run: `cd agent && .venv/bin/pytest tests/test_control_loop.py tests/test_main.py -q`

- [ ] **Step 3: Implement a supervisor wrapper and runtime timestamps**

The supervisor owns client construction inside its retry loop, updates timestamps only after successful sync/telemetry, redacts error details, and remains cancellable during FastAPI shutdown.

- [ ] **Step 4: Extend `/health`**

For `control_mode=off`, keep the current health contract. For observe/apply, return HTTP 503 when the supervisor is dead or no successful control activity has occurred within the configured stale window; include generation/digest presence and Xray readiness without secrets.

- [ ] **Step 5: Run focused and full agent tests**

Run: `cd agent && .venv/bin/pytest -q`

### Task 5: Verification and package commit

**Files:**
- Modify: `docs/superpowers/plans/2026-08-01-audit-correctness-p1.md` only to mark completed checkboxes if desired.

**Interfaces:**
- Consumes all prior task behavior.
- Produces one deployable P1 correctness package.

- [ ] **Step 1: Run formatting and lint**

Run: `cd agent && .venv/bin/ruff format --check app tests && .venv/bin/ruff check app tests`

- [ ] **Step 2: Run the full agent suite**

Run: `cd agent && .venv/bin/pytest -q`

- [ ] **Step 3: Run the repository tests that cover control contracts**

Run: `cd bot && .venv/bin/pytest tests/test_pull_control_workflows.py tests/test_node_control_api.py -q`

- [ ] **Step 4: Review the diff for secrets and unrelated changes**

Run: `git diff --check && git status --short && git diff -- agent docs/superpowers/plans/2026-08-01-audit-correctness-p1.md`

- [ ] **Step 5: Commit the verified package**

```bash
git add agent docs/superpowers/plans/2026-08-01-audit-correctness-p1.md
git commit -m "fix(agent): make control failures fail safe"
```
