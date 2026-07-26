# Full Hardening, Resilience, and Cascade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden AegisVPN's subscription/site surface, make node builds and Hy2 certificates reproducible, add a highly available central control plane, and ship dormant cascade support for a future Russian entry node.

**Architecture:** Deliver three independently revertible workstreams in one release cycle. Preserve subscription tokens, device UUIDs, Reality identities, and per-location transport choices; roll out website changes on Poland, certificate automation on USA, database failover on Poland/USA/Germany, and cascade schema v2 in a disabled state.

**Tech Stack:** Python 3.14, aiohttp, FastAPI, SQLAlchemy, aiogram, React 18, Vite/Vitest, Caddy, Docker Compose, Xray, Hysteria2, PostgreSQL, Patroni, etcd, Cloudflare DNS API.

## Global Constraints

- `Support-Url` is `https://t.me/AegisVPNsupportBot`.
- `Profile-Web-Page-Url` is `https://aegisvpn.org`.
- Unlimited installed devices and existing simultaneous-connection limits remain unchanged.
- VLESS remains default; Hy2 appears only after an explicit per-location user selection.
- Existing subscription URLs, UUIDs, Reality keys, and active sessions remain valid.
- WireGuard is not introduced.
- Every behavior change follows a witnessed red/green test cycle.
- User-owned untracked `AGENTS.md` and `landing/` are not modified.

---

### Task 1: Subscription Metadata

**Files:**
- Modify: `bot/src/core/config.py`
- Modify: `bot/src/main.py`
- Create: `bot/tests/test_subscription_response.py`
- Modify: `deploy/vps/bot.env.example`

**Interfaces:**
- Produces: `Settings.support_public_url: str` and existing `Settings.site_public_url`.
- Produces: `subscription_response()` headers consumed by Happ/v2ray clients.

- [ ] **Step 1: Write the failing response test**

Create an aiohttp request fixture around a real temporary subscription and assert:

```python
assert response.headers["Support-Url"] == "https://t.me/AegisVPNsupportBot"
assert response.headers["Profile-Web-Page-Url"] == "https://aegisvpn.org"
assert "AegisEcoVPN_bot" not in response.headers["Support-Url"]
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_subscription_response.py -q`

Expected: failure because `Support-Url` still uses the main bot and the web-page header is absent.

- [ ] **Step 3: Implement settings and headers**

Add:

```python
support_public_url: str = "https://t.me/AegisVPNsupportBot"
site_public_url: str = "https://aegisvpn.org"
```

Set both response headers from those normalized settings. Keep
`BOT_PUBLIC_URL` only for main-bot/payment uses.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_subscription_response.py -q`

- [ ] **Step 5: Commit**

Commit: `fix(subscription): advertise support bot and website`

### Task 2: Private Website API and Correct Privacy Disclosure

**Files:**
- Modify: `bot/src/api/main.py`
- Modify: `bot/src/api/auth.py`
- Modify: `bot/src/api/checkout.py`
- Modify: `bot/src/core/terms.py`
- Modify: `bot/src/privacy/privacy_ru.md`
- Modify: `bot/src/privacy/privacy_en.md`
- Modify: `bot/tests/test_web_auth.py`
- Create: `bot/tests/test_site_api_security.py`
- Modify: `bot/tests/test_checkout.py`

**Interfaces:**
- Produces: authenticated `GET /api/avatar/me`.
- Produces: `no-store` account/auth/checkout responses.
- Produces: `validate_provider_redirect(url: str, method: str) -> str`.

- [ ] **Step 1: Write failing API security tests**

Assert literal behavior:

```python
assert client.get("/api/avatar/me").status_code == 401
assert client.get("/api/me", cookies=valid_cookie).headers["cache-control"] == "no-store"
assert client.post("/api/auth/telegram", content=b"{bad", headers={"content-type": "application/json"}).status_code == 400
assert validate_provider_redirect("javascript:alert(1)", "sbp") raises ValueError
assert validate_provider_redirect("https://app.platega.io/pay/1", "sbp") == "https://app.platega.io/pay/1"
```

Use a real temporary database/user/avatar for the authenticated avatar case and
assert a different user's cookie cannot retrieve it.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_site_api_security.py tests/test_web_auth.py tests/test_checkout.py -q`

- [ ] **Step 3: Implement minimal API changes**

Replace `/api/avatar/{user_id}` with `/api/avatar/me`, reuse `read_session`,
return private cache headers, and update `_me_payload`. Catch JSON decoding/type
errors at both Telegram auth endpoints and return 400. Add a response helper that
sets `Cache-Control: no-store`. Validate provider schemes and method-specific
hosts before returning redirect URLs.

- [ ] **Step 4: Update privacy and legal version**

Document first/last name, photo URL, cached bytes/MIME, purpose, retention, and
deletion. Set:

```python
TERMS_VERSION = "2026-07-27"
```

- [ ] **Step 5: Verify GREEN and full backend regression**

Run: `uv run pytest tests/test_site_api_security.py tests/test_web_auth.py tests/test_checkout.py tests/test_terms_gate.py -q`

- [ ] **Step 6: Commit**

Commit: `fix(site): protect account data and refresh privacy consent`

### Task 3: Accessible Frontend, Legal Documents, and Accurate Copy

**Files:**
- Modify: `web/package.json`
- Modify: `web/package-lock.json`
- Create: `web/src/App.test.jsx`
- Modify: `web/src/App.jsx`
- Modify: `web/src/TelegramLogin.jsx`
- Modify: `web/src/i18n.js`
- Modify: `web/src/Globe.jsx`
- Create: `web/src/test/setup.js`
- Modify: `web/vite.config.js`

**Interfaces:**
- Produces: labelled modal with focus trap/restore and background lock.
- Produces: safe Markdown legal renderer.
- Produces: localized metadata updater.

- [ ] **Step 1: Install test/render dependencies**

Add pinned compatible versions of Vitest, jsdom, Testing Library,
`@testing-library/jest-dom`, `react-markdown`, and `remark-gfm`; update the lock
file through npm.

- [ ] **Step 2: Write failing UI tests**

Tests must assert real DOM behavior:

```jsx
expect(screen.getByRole('link', {name: 'Поддержка'})).toHaveAttribute(
  'href', 'https://t.me/AegisVPNsupportBot'
)
expect(screen.getByRole('dialog')).toHaveAttribute('aria-modal', 'true')
expect(document.body).toHaveStyle({overflow: 'hidden'})
expect(document.activeElement).toBe(screen.getByRole('button', {name: 'Close'}))
```

Also assert focus restoration, Tab wrapping, legal headings without literal
`<b>`/`**`, English title/description, and no-auto-renewal copy.

- [ ] **Step 3: Verify RED**

Run: `npm test -- --run`

- [ ] **Step 4: Implement minimal UI behavior**

Add a `Modal` ref/focus lifecycle and inert main root, localized `ariaLabel`,
safe ReactMarkdown legal rendering, footer legal/support links, iframe title
mutation observer, decorative-canvas ARIA, 44px targets, and metadata effects.

- [ ] **Step 5: Verify GREEN**

Run: `npm test -- --run`

- [ ] **Step 6: Commit**

Commit: `fix(web): harden legal flows and dialog accessibility`

### Task 4: Localized Production Entries and Security Headers

**Files:**
- Create: `web/scripts/localize-dist.mjs`
- Create: `web/scripts/localize-dist.test.mjs`
- Create: `web/public/favicon.svg`
- Create: `web/public/robots.txt`
- Create: `web/public/sitemap.xml`
- Modify: `web/package.json`
- Modify: `web/index.html`
- Modify: `deploy/vps/Caddyfile`
- Create: `deploy/vps/tests/test_site_caddy.py`

**Interfaces:**
- Produces: `dist/ru/index.html`, `dist/en/index.html`, and static SEO files.
- Produces: Caddy security and cache headers without changing `/sub/*`.

- [ ] **Step 1: Write failing build and Caddy tests**

Run the localizer against a temporary HTML fixture and assert literal RU/EN
lang/title/description/canonical/hreflang output. Adapt the Caddyfile and run
`caddy validate` in a container; make HTTPS test requests and assert HSTS, CSP,
nosniff, referrer, permissions, frame-ancestors, and no-store preservation.

- [ ] **Step 2: Verify RED**

Run: `node --test scripts/localize-dist.test.mjs`

Run: `uv run pytest tests/test_site_caddy.py -q` from `deploy/vps`.

- [ ] **Step 3: Implement localizer/static assets/Caddy headers**

Make `npm run build` execute Vite then the localizer. Add a CSP limited to self,
`https://telegram.org`, and `https://oauth.telegram.org`; allow inline styles
only. Add one-year non-preload HSTS and remove implementation headers.

- [ ] **Step 4: Verify GREEN**

Run: `npm test -- --run && npm run build && npm audit --omit=dev`

Run: `uv run pytest tests/test_site_caddy.py -q`.

- [ ] **Step 5: Commit**

Commit: `fix(web): add localized SEO entries and HTTP hardening`

### Task 5: Reproducible Runtime Pins

**Files:**
- Modify: `agent/Dockerfile`
- Modify: `bot/Dockerfile.deploy`
- Modify: `support_bot/Dockerfile`
- Modify: `deploy/vps/docker-compose.yml`
- Create: `deploy/vps/runtime-versions.env`
- Create: `deploy/vps/tests/test_runtime_pins.py`
- Modify: `deploy/vps/update.py`

**Interfaces:**
- Produces: digest/version-only build inputs.
- Produces: `update.py --verify-runtime-pins`.

- [ ] **Step 1: Capture working production versions/digests read-only**

Read `xray version`, Hy2 version, container image IDs/repo digests, Caddy
version, Python image ID, and uv version from Poland/USA. Do not pull or restart.

- [ ] **Step 2: Write failing pin tests**

Parse the Dockerfiles/Compose/runtime env and fail on `latest`, unverified remote
archives, missing `uv.lock`, or `uv sync` without `--frozen`. Execute
`--verify-runtime-pins` against valid and checksum-mismatch fixtures.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/test_runtime_pins.py -q`.

- [ ] **Step 4: Implement pins and checksum verification**

Pin the captured known-working versions/digests, copy every lock file, use
`--frozen`, and verify Xray SHA-256 before unzip. Keep architecture selection
explicit.

- [ ] **Step 5: Verify GREEN and build images**

Run: `uv run pytest tests/test_runtime_pins.py -q`.

Run Compose builds for Agent, bot, and support bot without deploying them.

- [ ] **Step 6: Commit**

Commit: `build: pin and verify production runtimes`

### Task 6: Automatic Hysteria2 Certificate Delivery

**Files:**
- Create: `bot/src/control/certificates.py`
- Modify: `bot/src/control/router.py`
- Modify: `bot/src/core/config.py`
- Create: `bot/tests/test_node_certificate_api.py`
- Create: `agent/app/certificate_sync.py`
- Modify: `agent/app/control_client.py`
- Create: `agent/tests/test_certificate_sync.py`
- Create: `deploy/vps/export_hy2_certificate.py`
- Create: `deploy/vps/systemd/aegis-hy2-cert-export.service`
- Create: `deploy/vps/systemd/aegis-hy2-cert-export.timer`
- Modify: `deploy/vps/docker-compose.yml`
- Modify: `deploy/vps/update.py`
- Modify: `deploy/vps/tests/test_control_plane_deploy.py`

**Interfaces:**
- Produces: authenticated `GET /api/node/v1/hy2-certificate`.
- Produces: `CertificateSynchronizer.check_once() -> CertificateSyncResult`.
- Produces: root-owned exported PEM bundle outside the database.

- [ ] **Step 1: Write failing API and Agent tests**

Cover unauthenticated/cross-node denial, disabled-Hy2 denial, no-store, matching
certificate/key/hostname, expiry thresholds, unchanged fingerprint no-op,
atomic replacement, rollback on invalid PEM, and restart only on change.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_node_certificate_api.py -q` in `bot`.

Run: `uv run pytest tests/test_certificate_sync.py -q` in `agent`.

- [ ] **Step 3: Implement endpoint, exporter, and Agent synchronizer**

Serve PEM only from the root-owned export directory after existing node auth.
Validate with `cryptography`, store temp files beside final files, fsync, chmod
0600, rename atomically, and restart only `aegis-hysteria`.

- [ ] **Step 4: Verify GREEN**

Run the two focused suites plus deploy tests.

- [ ] **Step 5: Commit**

Commit: `feat(hy2): distribute renewed certificates over node control`

### Task 7: PostgreSQL/Patroni Central Failover

**Files:**
- Create: `deploy/vps/ha/docker-compose.ha.yml`
- Create: `deploy/vps/ha/patroni.yml`
- Create: `deploy/vps/ha/etcd.env.example`
- Create: `deploy/vps/ha/haproxy.cfg`
- Create: `deploy/vps/ha/migrate_sqlite_to_postgres.py`
- Create: `deploy/vps/ha/verify_failover.py`
- Create: `deploy/vps/tests/test_ha_deployment.py`
- Modify: `bot/src/core/database.py`
- Create: `bot/src/core/leader.py`
- Modify: `bot/src/scheduler/__init__.py`
- Modify: `bot/src/main.py`
- Modify: `support_bot/src/main.py`

**Interfaces:**
- Produces: PostgreSQL-backed application URL through local HAProxy.
- Produces: PostgreSQL advisory-lock singleton lease.
- Produces: verified SQLite-to-PostgreSQL migration and failover rehearsal.

- [ ] **Step 1: Write failing portability, leader, and HA tests**

Test PostgreSQL SQL generation/migrations, advisory-lock acquisition/loss,
singleton scheduler suppression, migration row/count/digest equality, Patroni
three-member topology, and failover verifier rejection of two writable nodes.

- [ ] **Step 2: Verify RED**

Run focused bot and deploy HA suites.

- [ ] **Step 3: Implement HA topology and migration**

Use Poland/USA PostgreSQL members and Poland/USA/Germany etcd quorum. Route
applications through local HAProxy to the Patroni leader. Wrap Telegram polling,
support polling, and scheduler startup in renewable PostgreSQL advisory locks.
Keep stateless site/control APIs on both hosts.

- [ ] **Step 4: Verify GREEN in local containers**

Start a disposable three-member test topology, migrate a copied fixture DB,
force preferred-primary loss, and assert exactly one writable leader plus
unchanged row digests.

- [ ] **Step 5: Commit**

Commit: `feat(control): add PostgreSQL active-passive failover`

### Task 8: DNS-Only Cloudflare Failover

**Files:**
- Create: `deploy/vps/ha/cloudflare_failover.py`
- Create: `deploy/vps/ha/cloudflare.env.example`
- Create: `deploy/vps/ha/patroni_callback.sh`
- Create: `deploy/vps/tests/test_cloudflare_failover.py`
- Modify: `docs/control-plane-operations.md`

**Interfaces:**
- Produces: idempotent DNS-only A/AAAA record update restricted to the Aegis zone.
- Consumes: scoped `CLOUDFLARE_API_TOKEN`, zone ID, host records, Patroni role,
  and local health.

- [ ] **Step 1: Write failing DNS callback tests**

Use a local HTTP fake with complete Cloudflare-shaped responses. Assert no
update for a replica/unhealthy host, proxied is always false, TTL is 60, only
configured records change, secrets never print, and repeated calls are no-ops.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_cloudflare_failover.py -q`.

- [ ] **Step 3: Implement callback and manual rollback**

Require leader and local application health before API mutation. Emit structured
redacted logs and preserve previous record values for the documented rollback
command.

- [ ] **Step 4: Verify GREEN**

Run focused tests, then a Cloudflare API read-only token check. Do not mutate
production DNS until the standby rehearsal passes.

- [ ] **Step 5: Commit**

Commit: `feat(deploy): add DNS-only control-plane failover`

### Task 9: Cascade Schema v2 and Dormant Data Plane

**Files:**
- Modify: `bot/src/models/server.py`
- Create: `bot/src/models/cascade.py`
- Modify: `bot/src/models/__init__.py`
- Modify: `bot/src/core/migrations.py`
- Modify: `bot/src/control/schemas.py`
- Modify: `bot/src/control/state.py`
- Modify: `bot/src/services/subscription_service.py`
- Create: `bot/src/services/cascade_service.py`
- Create: `bot/tests/test_cascade_control.py`
- Create: `bot/tests/test_cascade_subscription.py`
- Modify: `agent/app/control_client.py`
- Create: `agent/app/cascade.py`
- Create: `agent/tests/test_cascade_reconciliation.py`
- Modify: `docs/control-plane-operations.md`

**Interfaces:**
- Produces: node roles `entry|exit|both`.
- Produces: versioned cascade route snapshot payload.
- Produces: fail-closed Xray entry→ordered-exit configuration.

- [ ] **Step 1: Write failing schema/config tests**

Assert v1 clients ignore disabled v2 routes, nodes cannot read another node's
service identity, unacknowledged routes never enter subscriptions, generated
entry outbounds use VLESS+Reality+XHTTP, all-exit failure has no `direct`
fallback, offline route revocation converges, and direct/Hy2 preferences remain
unchanged.

- [ ] **Step 2: Verify RED**

Run focused bot and Agent cascade suites.

- [ ] **Step 3: Implement schema, migration, snapshot, and reconciliation**

Add roles/routes/service identities, v2 payload validation, entry balancer and
observatory generation, atomic Agent apply, ACK gating, and subscription
suppression. Default every route to disabled.

- [ ] **Step 4: Verify GREEN and simulate**

Run focused suites, then a namespace/container simulation with one entry and two
exits. Verify exit failover and fail-closed behavior.

- [ ] **Step 5: Commit**

Commit: `feat(cascade): add dormant Reality XHTTP entry routes`

### Task 10: Full Verification and Production Rollout

**Files:**
- Modify only when a failing verification exposes a regression.

**Interfaces:**
- Produces: verified commit series on `main` and deployed version checkpoints.

- [ ] **Step 1: Run complete local verification**

Run all Agent, bot, deploy, support-bot, and frontend suites; production frontend
build; npm audit; Docker builds; `git diff --check`; and `graphify update .`
when Graphify is available.

- [ ] **Step 2: Deploy Workstream 1 to Poland**

Back up DB/config/site, deploy, reload Caddy, and verify subscription headers,
RU/EN entries, static files, security headers, login modal, API auth errors, and
fresh node heartbeat. Existing user subscriptions must remain byte-compatible
apart from response metadata.

- [ ] **Step 3: Deploy Workstream 2 to USA then fleet**

Deploy pinned images/certificate sync to USA, verify VLESS, Hy2 auth/data and
certificate fingerprint, then roll Germany, Hong Kong, Switzerland, and
Finland.

- [ ] **Step 4: Rehearse and cut over Workstream 3**

Build HA standby/quorum, migrate a copied DB, rehearse failover and restore,
obtain the scoped Cloudflare token, take a final backup, perform the controlled
write cutover, and verify RPO/RTO. Keep all cascade routes disabled.

- [ ] **Step 5: Final audit and push**

Verify production from desktop/mobile and every node, compare desired/applied
generations and digests, confirm no public Agent port, inspect logs for secrets,
push `main`, and record rollback checkpoints.
