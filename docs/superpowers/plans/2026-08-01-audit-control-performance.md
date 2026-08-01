# Audit Control-Plane Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove steady-state full snapshot rebuilds and `O(U×K)` traffic scans while bounding snapshot history.

**Architecture:** Mutation paths remain responsible for publishing immutable snapshots. Node `/sync` performs a cheap latest-generation lookup, rebuilding only when no manifest exists or acknowledged capabilities change the desired schema. Heartbeats are throttled, acknowledged history is pruned to a small retry window, and traffic stats are indexed once by subscription.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async, SQLite/PostgreSQL, pytest.

## Global Constraints

- Keep the existing snapshot manifest/page/ACK wire format.
- Do not require Redis or PostgreSQL for correctness.
- Keep the latest acknowledged generation plus two older generations and every newer unacknowledged generation.
- Preserve first-sighting traffic baselines and Xray-restart delta semantics.
- Use TDD for every behavior change.

---

### Task 1: Cheap sync polling and throttled heartbeat

**Files:**
- Modify: `bot/src/core/config.py`
- Modify: `bot/src/control/state.py`
- Modify: `bot/src/control/router.py`
- Test: `bot/tests/test_node_control_api.py`

**Interfaces:**
- Produces: `latest_snapshot(session, server_id) -> NodeSnapshot | None`
- Adds: `node_control_heartbeat_seconds: float = 30.0`

- [x] Add a failing test that spies on `publish_snapshot`, runs a no-change long-poll through multiple iterations, and asserts at most one build for initial state and zero rebuilds once a manifest exists.
- [x] Add a failing test that two sync requests inside the heartbeat interval do not change `control_last_seen_at`, while a request after the interval does.
- [x] Implement `latest_snapshot()` as one indexed descending-generation query.
- [x] In `/sync`, publish only when no snapshot exists or the reported capability set changed; otherwise read latest only.
- [x] Update agent version/capabilities only when values differ and `last_seen` only after the configured interval.
- [x] Run: `cd bot && .venv/bin/pytest tests/test_node_control_api.py tests/test_node_desired_state.py -q`

### Task 2: Snapshot retention after ACK

**Files:**
- Modify: `bot/src/control/state.py`
- Modify: `bot/src/control/router.py`
- Test: `bot/tests/test_node_control_api.py`
- Test: `bot/tests/test_node_desired_state.py`

**Interfaces:**
- Produces: `prune_acknowledged_snapshots(session, server_id, acknowledged_generation, keep_previous=2) -> int`

- [x] Add a failing test that creates generations 1–6, ACKs generation 6, and observes generations 4–6 retained.
- [x] Add a failing test that a newer unacknowledged generation is never deleted when an older generation is ACKed.
- [x] Delete pages and snapshots with `generation < acknowledged_generation - keep_previous` explicitly, so SQLite foreign-key pragma state cannot leak orphan pages.
- [x] Call pruning only after a successful non-duplicate ACK and before commit.
- [x] Run: `cd bot && .venv/bin/pytest tests/test_node_control_api.py tests/test_node_desired_state.py -q`

### Task 3: Linear traffic aggregation

**Files:**
- Modify: `bot/src/scheduler/tasks.py`
- Test: `bot/tests/test_poll_traffic.py`

**Interfaces:**
- Produces: `_index_stats_by_subscription(stats: dict) -> dict[tuple[int, int], list[tuple[str, dict]]]`

- [x] Add literal parser tests for base/device emails and malformed/unrelated keys.
- [x] Add a failing behavior test using a dict that raises if iterated more than once; `poll_traffic()` must still update both links correctly.
- [x] Parse each stat email once with a strict regex for `user_<uid>_sub_<sid>` and optional `_dev_<did>`.
- [x] Replace each link's full-dict scan with an `O(1)` index lookup while preserving cursors, baselines, and restart handling.
- [x] Run: `cd bot && .venv/bin/pytest tests/test_poll_traffic.py tests/test_pull_control_workflows.py -q`

### Task 4: Verification and commit

**Files:**
- Include only the files above and this plan.

- [x] Run Ruff formatting/checks for every changed source and test file.
- [x] Run: `cd bot && .venv/bin/pytest -q`
- [x] Run: `git diff --check && git status --short`
- [ ] Commit: `git commit -m "perf(control): avoid steady-state snapshot rebuilds"`
