# Agent Pull Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace public bot-to-node Agent calls with an outbound, versioned and self-healing node sync channel over HTTPS/443.

**Architecture:** The bot database publishes immutable, paginated desired-state snapshots per server. Each agent authenticates to a dedicated mTLS control hostname, long-polls for a newer generation, verifies the snapshot digest, reconciles Xray and Hysteria locally, and acknowledges only a fully applied generation. The current push API remains available during a per-node observation/canary rollout and is disabled only after pull mode is verified.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, SQLite/PostgreSQL, aiohttp, Caddy, pytest, Docker Compose.

## Global Constraints

- Do not impose a device-count limit; snapshot pagination bounds work per request.
- Use outbound TLS on TCP/443; do not add WireGuard.
- Keep the existing Agent API on loopback for Hysteria authentication and diagnostics.
- Do not interrupt Xray for normal additions or removals; retain reload only as the existing live-API fallback.
- Treat the bot database as the source of truth and reconcile complete state after an outage.
- Never log or commit node tokens, UUID lists, private keys, or certificate material.
- Roll out one node at a time and retain the existing push path until each node acknowledges pull state.

---

## File Map

### Bot/control service

- `bot/src/models/node_control.py`: immutable snapshot manifests/pages and node telemetry records.
- `bot/src/models/server.py`: node identity, mode, desired/applied generation, digest and last-contact metadata.
- `bot/src/control/schemas.py`: versioned request/response models shared by control endpoints.
- `bot/src/control/auth.py`: proxy-bound mTLS fingerprint and token authentication.
- `bot/src/control/state.py`: canonical desired-state construction, pagination, digesting and publication.
- `bot/src/control/router.py`: sync, page, acknowledgement and telemetry endpoints.
- `bot/src/control/__init__.py`: control package exports.
- `bot/src/api/main.py`: include the node-control router.
- `bot/src/core/config.py`: trusted-proxy secret and long-poll/page bounds.
- `bot/src/core/migrations.py`: additive columns for existing `servers` tables.
- `bot/src/services/node_control_service.py`: mark/publish affected server state and query pull telemetry.
- `bot/src/services/subscription_service.py`: retain push in legacy/observe mode and publish desired state for pull nodes.
- `bot/src/services/server_access_service.py`: publish exact post-reconcile state, including all device UUIDs.
- `bot/src/services/admin_service.py`: publish connection-limit changes.
- `bot/src/scheduler/tasks.py`: use pushed telemetry for pull nodes.

### Node agent

- `agent/app/control_models.py`: manifest, page, item, acknowledgement and telemetry models.
- `agent/app/control_client.py`: mTLS HTTP client, retry/backoff, long polling and page assembly.
- `agent/app/reconcile.py`: exact local desired-state reconciliation and durable applied-generation state.
- `agent/app/config.py`: control URL, node token/cert paths, mode and timeout settings.
- `agent/app/main.py`: start/stop the outbound sync task and keep HTTP loopback-only.
- `agent/app/xray.py`: atomic config persistence.
- `agent/app/connlimit.py`: atomically replace the complete override map.
- `agent/pyproject.toml` and `agent/uv.lock`: add aiohttp.
- `agent/entrypoint.sh`: configurable loopback bind host.

### Deployment

- `deploy/vps/Caddyfile`: dedicated mTLS control virtual host and trusted identity headers.
- `deploy/vps/docker-compose.yml`: control-service and agent certificate mounts/environment.
- `deploy/vps/bot.env.example`: control proxy secret and protocol bounds.
- `deploy/vps/vpn.env.example`: outbound control settings.
- `deploy/vps/add_server.py`: generate/register pull credentials without a public Agent URL.
- `deploy/vps/update.py`: observation/apply rollout and public Agent-port closure.
- `deploy/vps/control-plane/README.md`: CA, certificate rotation, canary and rollback runbook.

---

### Task 1: Persist node control identity and immutable snapshots

**Files:**
- Create: `bot/src/models/node_control.py`
- Modify: `bot/src/models/server.py`
- Modify: `bot/src/models/__init__.py`
- Modify: `bot/src/core/migrations.py`
- Test: `bot/tests/test_node_control_migrations.py`

**Interfaces:**
- Produces: `NodeSnapshot`, `NodeSnapshotPage`, `NodeTelemetry`.
- Produces: `Server.control_mode`, `control_token_hash`, `control_cert_fingerprint`, `desired_generation`, `applied_generation`, `applied_digest`, `control_last_seen_at`, `control_last_reconciled_at`, `control_last_error`, `control_agent_version`, `control_capabilities`.
- Consumes: existing `Base`, `utcnow`, `run_migrations()`.

- [ ] **Step 1: Write migration/model tests**

```python
async def test_control_columns_and_snapshot_tables_are_created_idempotently():
    await run_migrations()
    await run_migrations()
    assert {
        "control_mode",
        "control_token_hash",
        "control_cert_fingerprint",
        "desired_generation",
        "applied_generation",
        "applied_digest",
        "control_last_seen_at",
        "control_last_reconciled_at",
        "control_last_error",
        "control_agent_version",
        "control_capabilities",
    } <= await column_names("servers")
    assert {"node_snapshots", "node_snapshot_pages", "node_telemetry"} <= await table_names()
```

- [ ] **Step 2: Run the test and verify the missing schema**

Run: `cd bot && uv run pytest tests/test_node_control_migrations.py -q`

Expected: FAIL because the models/tables and server columns do not exist.

- [ ] **Step 3: Add the control models and additive migration columns**

Use these model contracts:

```python
class NodeSnapshot(Base):
    __tablename__ = "node_snapshots"
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer)
    page_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class NodeSnapshotPage(Base):
    __tablename__ = "node_snapshot_pages"
    server_id: Mapped[int] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    page_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_digest: Mapped[str] = mapped_column(String(64))
    items: Mapped[list[dict]] = mapped_column(JSON)


class NodeTelemetry(Base):
    __tablename__ = "node_telemetry"
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
```

Use `control_mode="push"` as the migration/default value so deploying the schema alone changes no runtime behavior.

- [ ] **Step 4: Run migration tests**

Run: `cd bot && uv run pytest tests/test_node_control_migrations.py tests/test_migrations.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/src/models bot/src/core/migrations.py bot/tests/test_node_control_migrations.py
git commit -m "feat(control): persist node sync state"
```

---

### Task 2: Build canonical paginated desired-state snapshots

**Files:**
- Create: `bot/src/control/__init__.py`
- Create: `bot/src/control/schemas.py`
- Create: `bot/src/control/state.py`
- Test: `bot/tests/test_node_desired_state.py`

**Interfaces:**
- Produces: `canonical_json(value: object) -> bytes`.
- Produces: `build_desired_items(session: AsyncSession, server_id: int) -> list[dict]`.
- Produces: `publish_snapshot(session: AsyncSession, server_id: int, page_size: int) -> NodeSnapshot`.
- Consumes: `Server`, `SubscriptionServer`, `Subscription`, `Device`, `User`, `NodeSnapshot`, `NodeSnapshotPage`.

- [ ] **Step 1: Write desired-state tests**

Cover an active base UUID, active devices, suspended/removed devices, an expired
subscription, and a per-user connection-limit override:

```python
items = await build_desired_items(session, server.id)
assert items == [
    {"kind": "client", "uuid": base_uuid, "email": "user_10_sub_20", "expire_ms": expires_ms},
    {"kind": "client", "uuid": device_uuid, "email": "user_10_sub_20_dev_30", "expire_ms": expires_ms},
    {"kind": "conn_limit", "user_id": 10, "limit": 3},
]
```

Also assert:

- stable ordering independent of insertion order;
- the same content returns the existing generation;
- changed content creates generation `N + 1`;
- every page contains at most the configured page size;
- concatenated page items hash to the manifest digest;
- no test creates or asserts a maximum number of devices.

- [ ] **Step 2: Run tests and verify missing snapshot functions**

Run: `cd bot && uv run pytest tests/test_node_desired_state.py -q`

Expected: FAIL on imports from `src.control.state`.

- [ ] **Step 3: Implement canonical desired-state construction**

Use these eligibility rules:

```python
active_subscription = (
    SubscriptionServer.server_id == server_id
    and Subscription.is_active
    and Subscription.expires_at > now_utc_naive
)
active_device = Device.is_active and not Device.is_suspended
```

Represent expiry as an integer Unix epoch in milliseconds. Emit one
`conn_limit` item only when `User.conn_limit` is not `None`; absence means the
agent removes any old override and falls back to its node default.

- [ ] **Step 4: Implement transactional snapshot publication**

Sort items by `(kind, stable key)`, encode with:

```python
json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
```

Lock the server row with `SELECT ... FOR UPDATE`, compare the digest with the
latest manifest, and create immutable pages plus a new generation only when the
digest changes. Retain the latest two acknowledged generations and every
unacknowledged generation.

- [ ] **Step 5: Run desired-state tests**

Run: `cd bot && uv run pytest tests/test_node_desired_state.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/src/control bot/tests/test_node_desired_state.py
git commit -m "feat(control): publish desired node snapshots"
```

---

### Task 3: Authenticate nodes and expose the versioned control API

**Files:**
- Create: `bot/src/control/auth.py`
- Create: `bot/src/control/router.py`
- Modify: `bot/src/control/schemas.py`
- Modify: `bot/src/api/main.py`
- Modify: `bot/src/core/config.py`
- Test: `bot/tests/test_node_control_api.py`

**Interfaces:**
- Produces: `authenticate_node(request: Request, session: AsyncSession) -> Server`.
- Produces endpoints:
  - `POST /api/node/v1/sync`
  - `GET /api/node/v1/snapshots/{generation}/pages/{page_index}`
  - `POST /api/node/v1/ack`
  - `POST /api/node/v1/telemetry`
- Consumes: `publish_snapshot()`, `NodeSnapshotPage`, `NodeTelemetry`.

- [ ] **Step 1: Write authentication and isolation tests**

```python
response = await client.post("/api/node/v1/sync", json=sync_body)
assert response.status_code == 401

response = await client.post(
    "/api/node/v1/sync",
    json=sync_body,
    headers={
        "X-Aegis-Proxy-Secret": proxy_secret,
        "X-Aegis-Node-Fingerprint": node_fingerprint,
        "Authorization": f"Bearer {node_token}",
    },
)
assert response.status_code == 200
```

Assert wrong proxy secret, fingerprint, token, inactive node, and cross-node page
access are rejected. Store/compare `sha256(node_token).hexdigest()` and use
`hmac.compare_digest`.

- [ ] **Step 2: Write protocol behavior tests**

Assert:

- sync returns a manifest only for a generation newer than `applied_generation`;
- page indices outside the manifest return 404;
- acknowledgement digest must match the immutable manifest;
- duplicate acknowledgement is a success;
- an older acknowledgement cannot lower `Server.applied_generation`;
- telemetry sequence numbers are monotonic and duplicate delivery is harmless;
- payload/page bounds return 413 or 422.

- [ ] **Step 3: Run tests and verify missing routes**

Run: `cd bot && uv run pytest tests/test_node_control_api.py -q`

Expected: FAIL with 404/import errors.

- [ ] **Step 4: Implement the authentication dependency**

Require all three headers, look up the active server by the normalized
certificate fingerprint, compare the high-entropy token digest in constant time,
and return only that server. Never accept `node_id` from a request body as
authorization.

- [ ] **Step 5: Implement sync, page, ack and telemetry routes**

Use a long-poll deadline of `settings.node_control_long_poll_seconds` and poll
the database at `settings.node_control_poll_interval_seconds`. Open a fresh
session on each poll iteration so changes committed by the bot process are
visible to the site API process.

- [ ] **Step 6: Run API and web regression tests**

Run: `cd bot && uv run pytest tests/test_node_control_api.py tests/test_web_auth.py tests/test_checkout.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add bot/src/control bot/src/api/main.py bot/src/core/config.py bot/tests/test_node_control_api.py
git commit -m "feat(control): expose authenticated node sync API"
```

---

### Task 4: Download and verify desired state from the agent

**Files:**
- Create: `agent/app/control_models.py`
- Create: `agent/app/control_client.py`
- Modify: `agent/app/config.py`
- Modify: `agent/pyproject.toml`
- Modify: `agent/uv.lock`
- Test: `agent/tests/test_control_client.py`

**Interfaces:**
- Produces: `ControlClient.sync(applied: AppliedState) -> DesiredSnapshot | None`.
- Produces: `ControlClient.ack(generation: int, digest: str, result: ReconcileResult) -> None`.
- Produces: `ControlClient.send_telemetry(sequence: int, payload: dict) -> None`.
- Produces: `DesiredSnapshot(generation: int, digest: str, items: list[DesiredItem])`.
- Consumes: control endpoints defined in Task 3.

- [ ] **Step 1: Write client verification tests with a local fake server**

Assert:

- mTLS paths and node token are passed to `aiohttp`;
- pages are requested in order;
- a page digest mismatch rejects the snapshot;
- a final manifest digest mismatch rejects the snapshot;
- duplicate UUID items reject the snapshot;
- unsupported schema versions reject the snapshot;
- a control URL failure tries the next configured URL;
- response and decompressed-size limits are enforced.

- [ ] **Step 2: Run tests and verify missing client**

Run: `cd agent && uv run pytest tests/test_control_client.py -q`

Expected: FAIL on imports from `app.control_client`.

- [ ] **Step 3: Add settings and protocol models**

Add settings with safe defaults:

```python
control_mode: Literal["off", "observe", "apply"] = "off"
control_urls: str = ""
control_token: SecretStr | None = None
control_client_cert: str = "/data/control/client.crt"
control_client_key: str = "/data/control/client.key"
control_ca_cert: str = "/data/control/ca.crt"
control_timeout_seconds: int = 40
control_max_page_bytes: int = 1_048_576
control_max_snapshot_bytes: int = 64 * 1_048_576
```

Parse `control_urls` as a comma-separated ordered list. `off` must not require
credentials so existing deployments continue to boot.

- [ ] **Step 4: Implement the mTLS aiohttp client**

Create an `ssl.SSLContext` from the CA, load the node certificate/key, use the
bearer token header, enforce byte bounds before JSON parsing, validate every page
with Pydantic, and hash the exact canonical item list before returning it.

- [ ] **Step 5: Update the lockfile and run tests**

Run: `cd agent && uv lock`

Run: `cd agent && uv run pytest tests/test_control_client.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/app/control_models.py agent/app/control_client.py agent/app/config.py agent/pyproject.toml agent/uv.lock agent/tests/test_control_client.py
git commit -m "feat(agent): download verified desired state"
```

---

### Task 5: Reconcile complete state atomically on a node

**Files:**
- Create: `agent/app/reconcile.py`
- Modify: `agent/app/xray.py`
- Modify: `agent/app/connlimit.py`
- Test: `agent/tests/test_reconcile.py`
- Test: `agent/tests/test_xray.py`
- Test: `agent/tests/test_connlimit.py`

**Interfaces:**
- Produces: `reconcile_snapshot(snapshot: DesiredSnapshot, *, observe: bool) -> ReconcileResult`.
- Produces: `load_applied_state() -> AppliedState`.
- Produces: `save_applied_state(state: AppliedState) -> None`.
- Produces: `replace_overrides(overrides: dict[int, int]) -> None`.
- Consumes: `xray_api_add()`, `xray_api_remove()`, `save_xray_config()`, `hysteria.refresh_from_config()`, `hysteria.kick()`.

- [ ] **Step 1: Write exact-set reconciliation tests**

Use a config containing one retained UUID, one stale base UUID and one stale
device UUID. Assert that reconciliation:

- adds the missing desired UUID to every VLESS inbound;
- removes both stale UUIDs from every VLESS inbound;
- uses transport-specific client records;
- replaces, rather than merges, connection-limit overrides;
- refreshes Hysteria before kicking removed emails;
- writes no file and invokes no live API in observation mode;
- does not save the applied generation after any add/remove/persist failure;
- repeated reconciliation is a no-op.

- [ ] **Step 2: Run tests and verify missing reconciler**

Run: `cd agent && uv run pytest tests/test_reconcile.py -q`

Expected: FAIL on imports from `app.reconcile`.

- [ ] **Step 3: Make Xray config persistence atomic**

Write JSON to a temporary file in the destination directory, flush, `os.fsync`,
`os.replace`, and fsync the directory. Preserve the existing FastAPI error
mapping at the boundary.

- [ ] **Step 4: Add complete override replacement**

```python
def replace_overrides(overrides: dict[int, int]) -> None:
    global _overrides
    _overrides = {int(user_id): max(0, int(limit)) for user_id, limit in overrides.items()}
    _save_overrides()
```

- [ ] **Step 5: Implement reconciliation and durable applied state**

Persist `/data/control/applied-state.json` with generation and digest only after
Xray disk state, live state, Hysteria state and override state all succeed.
Write the file atomically with mode `0600`.

- [ ] **Step 6: Run agent reconciliation regression tests**

Run: `cd agent && uv run pytest tests/test_reconcile.py tests/test_xray.py tests/test_connlimit.py tests/test_hysteria.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent/app/reconcile.py agent/app/xray.py agent/app/connlimit.py agent/tests
git commit -m "feat(agent): reconcile authoritative node state"
```

---

### Task 6: Run observation/apply sync and push telemetry

**Files:**
- Modify: `agent/app/main.py`
- Modify: `agent/app/control_client.py`
- Test: `agent/tests/test_control_loop.py`

**Interfaces:**
- Produces: `control_loop(stop_event: asyncio.Event) -> None`.
- Consumes: `ControlClient`, `load_applied_state()`, `reconcile_snapshot()`, existing Xray/Hysteria statistics.

- [ ] **Step 1: Write loop lifecycle and recovery tests**

Assert:

- `control_mode=off` starts no task;
- `observe` downloads/compares but never acknowledges as applied;
- `apply` acknowledges only a successful reconciliation;
- HTTP errors use exponential backoff with full jitter capped at 60 seconds;
- a successful sync resets backoff;
- cancellation closes the aiohttp session;
- telemetry sequence persists across agent restart;
- secrets and UUIDs are absent from error logs.

- [ ] **Step 2: Run tests and verify no control loop**

Run: `cd agent && uv run pytest tests/test_control_loop.py -q`

Expected: FAIL on the missing lifecycle functions.

- [ ] **Step 3: Implement the supervised control loop**

Create the task during FastAPI lifespan/startup, retain its handle, cancel and
await it during shutdown, and prevent one failed iteration from terminating the
agent process.

- [ ] **Step 4: Add bounded telemetry**

Send applied generation/digest, agent version, capabilities, online emails,
traffic counters and a redacted last reconciliation result. Limit the number of
telemetry records per request and split traffic records into bounded pages when
necessary.

- [ ] **Step 5: Run the complete agent suite**

Run: `cd agent && uv run pytest -q`

Run: `cd agent && uv run ruff check app tests`

Expected: all tests and lint checks PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/app agent/tests
git commit -m "feat(agent): run outbound control sync"
```

---

### Task 7: Make pull mode authoritative in bot workflows

**Files:**
- Create: `bot/src/services/node_control_service.py`
- Modify: `bot/src/services/subscription_service.py`
- Modify: `bot/src/services/server_access_service.py`
- Modify: `bot/src/services/admin_service.py`
- Modify: `bot/src/scheduler/tasks.py`
- Test: `bot/tests/test_pull_control_workflows.py`
- Test: `bot/tests/test_revoke_removes_devices.py`

**Interfaces:**
- Produces: `NodeControlService.publish_for_servers(session, server_ids)`.
- Produces: `NodeControlService.telemetry_for(server_id) -> dict | None`.
- Consumes: `publish_snapshot()`, `Server.control_mode`.

- [ ] **Step 1: Write dual-mode workflow tests**

Assert:

- `push` nodes retain the existing AgentClient behavior;
- `observe` nodes retain push and publish a comparison snapshot;
- `pull` nodes publish state and make no remote AgentClient mutation;
- subscription revoke removes base and every device UUID from desired state;
- an offline pull node receives the removal snapshot after its next sync;
- suspend/remove device and connection-limit changes publish a new generation;
- statistics for pull nodes come from monotonic node telemetry;
- an inactive server rejects sync and is omitted from user subscriptions.

- [ ] **Step 2: Run tests and verify legacy-only behavior**

Run: `cd bot && uv run pytest tests/test_pull_control_workflows.py tests/test_revoke_removes_devices.py -q`

Expected: FAIL because services always invoke `AgentClient`.

- [ ] **Step 3: Implement a single dual-mode boundary**

Use:

```python
if server.control_mode in {"observe", "pull"}:
    await NodeControlService.publish_for_servers(session, {server.id})
if server.control_mode in {"push", "observe"}:
    await AgentClient(server.agent_url, server.agent_token).add_client(...)
```

Apply the same boundary to additions, removals, device lifecycle, access
reconciliation and connection-limit changes. Desired state is generated from the
database, so removal publication occurs after the database mutation is flushed.

- [ ] **Step 4: Switch pull-node statistics to telemetry**

Keep legacy Agent polling for `push`/`observe`. For `pull`, consume the latest
monotonic telemetry payload and preserve the existing traffic-delta accounting
rules.

- [ ] **Step 5: Run focused and full bot tests**

Run: `cd bot && uv run pytest tests/test_pull_control_workflows.py tests/test_revoke_removes_devices.py tests/test_poll_traffic.py -q`

Run: `cd bot && uv run pytest -q`

Expected: focused tests PASS; the full suite has no new failure or hang compared
with the recorded baseline.

- [ ] **Step 6: Commit**

```bash
git add bot/src/services bot/src/scheduler/tasks.py bot/tests
git commit -m "feat(control): make pull state authoritative"
```

---

### Task 8: Provision mTLS and close the public Agent API safely

**Files:**
- Modify: `deploy/vps/Caddyfile`
- Modify: `deploy/vps/docker-compose.yml`
- Modify: `deploy/vps/bot.env.example`
- Modify: `deploy/vps/vpn.env.example`
- Modify: `deploy/vps/add_server.py`
- Modify: `deploy/vps/update.py`
- Modify: `agent/entrypoint.sh`
- Create: `deploy/vps/control-plane/README.md`
- Test: `deploy/vps/tests/test_control_plane_deploy.py`

**Interfaces:**
- Produces: per-node certificate/key/token provisioning with private files mode `0600`.
- Produces: Caddy control host that injects trusted fingerprint/proxy-secret headers.
- Produces: rollout commands for `push -> observe -> pull`.
- Consumes: control endpoint and agent settings from Tasks 3–7.

- [ ] **Step 1: Write deployment rendering tests**

Assert generated configuration:

- uses `https://<control-host>` without a non-standard port;
- mounts client key/certificate/CA read-only into the agent;
- never prints token/private-key values;
- starts new nodes in `observe`;
- binds Uvicorn to `127.0.0.1` after promotion to `pull`;
- removes public TCP/8444 only after the desired/applied generation check passes;
- preserves TCP/443 Xray and UDP/443 Hysteria rules;
- has an idempotent rollback restricted to the fixed control-server IP.

- [ ] **Step 2: Run deploy tests and verify current public defaults**

Run: `uv run pytest deploy/vps/tests/test_control_plane_deploy.py -q`

Expected: FAIL because `add_server.py` defaults to public
`http://<node>:8444` and the agent binds `0.0.0.0`.

- [ ] **Step 3: Add the dedicated Caddy mTLS virtual host**

Use a separate SNI hostname. Require certificates signed by the control CA,
remove any inbound identity headers, then set the validated certificate
fingerprint and a proxy secret on the loopback upstream request. Keep the
website virtual host unchanged.

- [ ] **Step 4: Add credential provisioning and rotation**

Generate one client key/certificate and one 32-byte URL-safe token per node.
Store node private material only under `/root/aegis/deploy/vps/data/control/`
with directory mode `0700` and file mode `0600`. Store only token hash and
certificate fingerprint in the central database.

- [ ] **Step 5: Implement guarded promotion and rollback**

Promotion must verify:

```text
control_last_seen_at is recent
desired_generation == applied_generation
applied_digest matches the desired snapshot
last_control_error is empty
```

Only then set `control_mode=pull`, bind the Agent API to loopback, recreate the
agent container without touching Xray, and close public TCP/8444. Rollback may
temporarily allow TCP/8444 only from the fixed control-server IP.

- [ ] **Step 6: Run deployment tests and static validation**

Run: `uv run pytest deploy/vps/tests/test_control_plane_deploy.py -q`

Run: `docker compose -f deploy/vps/docker-compose.yml config --quiet`

Run: `python -m compileall -q deploy/vps`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add deploy/vps agent/entrypoint.sh
git commit -m "feat(deploy): provision private outbound control plane"
```

---

### Task 9: End-to-end security and recovery verification

**Files:**
- Modify: `README.md`
- Modify: `agent/README.md`
- Create: `docs/control-plane-operations.md`
- Create: `bot/tests/test_node_control_recovery.py`

**Interfaces:**
- Consumes all preceding tasks.
- Produces operator-visible rollout, monitoring, credential rotation and incident procedures.

- [ ] **Step 1: Add the offline-revocation integration test**

Create a subscription with base and device UUIDs, sync generation N, simulate an
offline agent, revoke the subscription, publish generation N+1, reconnect, and
assert both UUIDs are absent locally before acknowledgement N+1 succeeds.

- [ ] **Step 2: Add recovery/security tests**

Cover certificate deactivation, token rotation overlap, stale generation replay,
corrupt local applied-state recovery, interrupted atomic file write, telemetry
replay, and a site API restart during long polling.

- [ ] **Step 3: Run all automated verification**

Run: `cd bot && uv run pytest -q`

Run: `cd bot && uv run ruff check src tests`

Run: `cd agent && uv run pytest -q`

Run: `cd agent && uv run ruff check app tests`

Run: `uv run pytest deploy/vps/tests -q`

Expected: no new failure relative to baseline; every new control-plane test PASS.

- [ ] **Step 4: Document operation and rollback**

Document exact commands and checks for CA setup, node enrollment, observation,
promotion, credential rotation, emergency node deactivation, control endpoint
failover and temporary fixed-IP rollback. Explicitly state that unlimited
devices remain supported.

- [ ] **Step 5: Run Graphify update**

Run: `graphify update .`

Expected: graph updates successfully. If the local `graphify` executable remains
unavailable, record that fact in the handoff without substituting a generated
graph.

- [ ] **Step 6: Commit**

```bash
git add README.md agent/README.md docs bot/tests/test_node_control_recovery.py
git commit -m "docs: add control plane operations runbook"
```
