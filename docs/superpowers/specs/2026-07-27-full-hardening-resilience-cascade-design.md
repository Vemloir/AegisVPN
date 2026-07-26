# AegisVPN Full Hardening, Resilience, and Cascade Design

Date: 2026-07-27

## Objective

Deliver the three approved workstreams in one release cycle:

1. correct subscription metadata and harden the public website, API, and legal
   flows;
2. make production builds reproducible and automate Hysteria2 certificate
   distribution;
3. remove the central VPS as a single point of failure and add dormant,
   testable support for a future Russian-entry cascaded VPN.

The release is implemented and reviewed in that order, with a separately
revertible commit series and a canary gate between workstreams. Existing
subscription URLs, VLESS identities, Reality identities, and user transport
preferences must remain valid.

## Confirmed Product Decisions

- A subscription may be installed on an unlimited number of devices. The
  existing simultaneous-connection limit remains separate.
- Per-location protocol selection remains authoritative. VLESS is the default;
  Hysteria2 is emitted only when the user explicitly selects it for that
  location. The subscription must not contain duplicate VLESS and Hysteria2
  entries for one location.
- `Support-Url` is `https://t.me/AegisVPNsupportBot`.
- `Profile-Web-Page-Url` is `https://aegisvpn.org`.
- The legal-document version is bumped after the privacy text is corrected.
  Every existing user must accept the new version before gated bot or payment
  actions.
- WireGuard is not used for node control or cascade traffic.
- A Russian entry node is not currently available. Cascade support is
  implemented, tested, and kept disabled until one is enrolled.

## Workstream 1: Subscription, Website, API, and Privacy

### Subscription response metadata

Subscription responses continue serving the same body and URL. The response
adds:

```text
Support-Url: https://t.me/AegisVPNsupportBot
Profile-Web-Page-Url: https://aegisvpn.org
```

Both values are settings with those production defaults so forks can override
them without editing source. The main-bot URL remains `BOT_PUBLIC_URL` and is
not reused as a support URL.

### Privacy and avatars

The privacy policy must list all profile data that the service persists:
Telegram ID, username, first name, last name, Telegram avatar URL, and the
cached avatar bytes and MIME type. It must state why the cached image exists,
how long it is retained, and that account deletion removes it.

The avatar route changes from a public sequential user-ID resource to an
authenticated current-user resource. `/api/me` returns
`/api/avatar/me?v=<content-digest>`. The endpoint verifies the signed session
cookie, rejects missing, expired, banned, or mismatched users, and returns
`Cache-Control: private, max-age=604800, immutable`. It never accepts an
arbitrary user ID.

`/api/me`, authentication responses, checkout responses, and other responses
containing account or subscription material use `Cache-Control: no-store`.

The legal version is changed from `2026-06-29` to the implementation date. The
existing version-pinned acceptance mechanism then forces re-acceptance without
a special migration.

### Website hardening

Caddy adds these response controls to the apex site and API:

- HSTS for one year with `includeSubDomains`, without preload;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: strict-origin-when-cross-origin`;
- a restrictive `Permissions-Policy`;
- clickjacking protection through CSP `frame-ancestors 'none'`;
- a CSP that permits the site's bundles, Telegram's Web App/Login scripts,
  Telegram's OAuth frame, same-origin API calls, and required images/fonts.

The CSP is first exercised on the control-host canary. It must not use a
wildcard source. Inline style support is temporarily allowed because the
current React UI is implemented with style attributes; script execution remains
restricted to self and the exact Telegram origins. Caller-supplied control-plane
identity headers continue to be overwritten only on the dedicated mTLS
hostname.

Caddy removes its own and upstream implementation headers where practical.
Hashed assets retain immutable caching; SPA HTML retains `no-cache`; private API
responses retain application-provided `no-store`.

### Website behavior and accessibility

- The footer's Support link opens `@AegisVPNsupportBot`.
- Terms and Privacy links are always available in the footer, before login or
  checkout.
- Legal Markdown is rendered as structured, escaped content. Raw HTML from the
  legacy privacy files is either converted to Markdown in the source documents
  or treated as text; it is never inserted with unrestricted
  `dangerouslySetInnerHTML`.
- Every modal is a labelled `role="dialog"` with `aria-modal="true"`.
- Opening a modal stores the prior focus, focuses its first useful control,
  traps Tab/Shift+Tab, locks background scrolling, and marks background content
  inert. Closing restores focus and all altered document state.
- The Telegram iframe receives a localized `title` after the widget injects it.
- Decorative canvases are `aria-hidden`; meaningful location text remains in
  the DOM.
- Interactive mobile targets are at least 44 by 44 CSS pixels.
- Language changes update `html[lang]`, document title, description, canonical,
  and alternate-language metadata.
- Production output contains localized `/ru/` and `/en/` entry documents so
  metadata is correct before JavaScript hydration.
- Real `favicon.svg`, `robots.txt`, and `sitemap.xml` files are served instead
  of the SPA fallback.
- “Отмена в любой момент” / “Cancel anytime” is replaced with accurate
  no-auto-renewal wording.

### API robustness

Malformed JSON on Telegram login and TMA login returns a structured 400 rather
than an unhandled 500. Authentication failures remain 401. Cross-origin
preflight remains denied. Telegram HMAC verification, the signed session format,
and `Secure`, `HttpOnly`, and `SameSite=Lax` cookie attributes are preserved.

The payment redirect URL returned by Telegram or Platega is validated against
HTTPS and an explicit provider-host allowlist before it reaches the browser.
Failed validation returns `provider_error`; `javascript:`, `data:`, HTTP, and
protocol-relative values are rejected. Payment return/failure URLs retain the
active `/ru/` or `/en/` route where the provider API permits it.

### Tests

Backend regression tests cover subscription headers, malformed JSON, private
avatars, no-store responses, legal-version behavior, and redirect validation.
Frontend tests use Vitest with Testing Library and axe-compatible assertions for
localized metadata, footer links, modal focus lifecycle, legal rendering, and
subscription purchase state. A production build test verifies localized entry
documents and static SEO files. Browser smoke tests cover 1280px desktop and
390px mobile layouts, Telegram iframe loading, language switching, and modal
keyboard behavior.

## Workstream 2: Reproducible Builds and Hysteria2 Certificates

### Reproducible containers

- Bot, Agent, and support-bot builds copy their committed `uv.lock` and run
  `uv sync --frozen --no-dev --no-install-project`.
- The deployment fails when `pyproject.toml` and the lock disagree.
- Python, uv, Xray, Hysteria2, Caddy, and MTProxy inputs are pinned to explicit
  versions. Remote binaries and images are pinned by SHA-256 or image digest.
- Xray downloads are architecture-aware and verified before extraction.
- A controlled update command refreshes pins, runs the full test matrix, and
  records the new versions. Normal node deployment never resolves `latest`.
- Services run with the minimum writable mounts and Linux capabilities that
  their current host-network/PID responsibilities permit. Bot, site API,
  support bot, and Caddy do not gain access to node private keys they do not use.

The initially pinned versions and digests are taken from the known-working
production containers before any image is rebuilt. This avoids changing runtime
versions during the hardening rollout.

### Certificate distribution

The current CA-signed Hysteria2 certificate remains centrally renewed by Caddy.
Its private key is not stored in database snapshots or committed configuration.

The control host exports the newest matching certificate and key into a
root-owned control directory whenever the Caddy-managed fingerprint changes.
An authenticated node-only endpoint exposes the bundle only when all of these
match:

1. the mTLS client certificate;
2. the per-node bearer token;
3. the Caddy-injected proxy secret;
4. a node record that is active, pull-enabled, and Hysteria2-capable.

The endpoint returns a fingerprint and expiry plus the PEM bundle with
`Cache-Control: no-store`. It is separate from paginated desired-state
snapshots, so private keys never enter SQLite, backups, telemetry, or logs.

The Agent periodically checks the bundle, validates hostname, validity dates,
certificate/key match, and minimum remaining lifetime, writes both files
atomically with mode 0600, and restarts only the Hysteria2 container when the
fingerprint changes. A failed validation keeps the last working certificate.
Telemetry reports only fingerprint, expiry, and update status.

A systemd timer on the control host performs the export check at least daily.
The Agent also checks on startup and at least every six hours. Alerts begin 21
days before expiry and become critical at 7 days.

### Tests and rollout

Tests cover digest pinning, checksum failures, bundle authorization, cross-node
isolation, expiry validation, atomic replacement, unchanged-fingerprint no-op,
and restart-on-change. The USA node is the Hysteria2 certificate canary; VLESS
is unaffected by the canary. After successful QUIC authentication and data
transfer checks, the bundle is enabled on the other nodes.

## Workstream 3: Central Resilience and Cascade Readiness

### Active/passive control plane

Poland remains the preferred control host and the USA VPS becomes the standby.
Germany is the third consensus member. The central database moves from SQLite
to PostgreSQL managed by Patroni with a three-member etcd quorum:

- Poland: preferred PostgreSQL primary, application stack, etcd member;
- USA: PostgreSQL streaming standby, complete application standby, etcd member;
- Germany: etcd witness only; it never stores the central application database.

Replication is asynchronous with WAL archiving and bounded lag alerts. Patroni
demotes a partitioned former primary when it cannot retain quorum, preventing
two writable databases. Existing compressed backups continue and a
PostgreSQL-native logical/physical backup is added before SQLite is retired.

The bot, support bot, payment reconciliation scheduler, and other singleton
workers use PostgreSQL advisory locks. Standby containers may run, but only the
lock holder consumes Telegram updates or executes singleton jobs. Website API,
subscription serving, payment callbacks, and node control API are stateless and
may run on both application hosts.

The existing aiohttp subscription and payment-callback handlers are separated
from the Telegram polling lifecycle so both control hosts can serve them. Both
control endpoints share the same generation history through PostgreSQL.
Every node retains ordered Poland and USA mTLS control URLs.

### DNS-only failover

`aegisvpn.org` remains DNS-only at Cloudflare; traffic is not proxied through
Cloudflare. A scoped Cloudflare API token may edit only A/AAAA records for the
AegisVPN zone. Patroni leader callbacks update the apex, `www`, subscription,
and control records to the writable application's healthy host with a 60-second
TTL. The callback requires both database leadership and local application
health. The token is supplied at deployment and never stored in the repository
or backups.

If the token is unavailable, the migration may build and verify the standby but
must not claim automatic failover. Manual promotion and DNS rollback commands
remain documented and tested.

The acceptance target is RPO below 30 seconds during normal replication and RTO
below 5 minutes including DNS cache expiry. Existing exit nodes continue
enforcing cached subscription expiries while both control hosts are unavailable.

### Cascade data model

The schema adds explicit node roles (`entry`, `exit`, `both`) and cascade routes.
A route contains an entry node, an ordered exit set, enabled state, display
label, health policy, and transport policy. Direct locations remain unchanged.
No route is advertised unless the entry and at least one exit have acknowledged
the same supported cascade schema.

Control snapshot schema v2 carries only the route-specific identities and Xray
configuration needed by the receiving node. A node cannot fetch another node's
private material. Schema v1 remains accepted during rolling deployment.

### Cascade data plane

The client-facing Russian entry uses VLESS + Reality + XHTTP on TCP/443, matching
the censorship-resistant transport already used by AegisVPN. The entry forwards
the route through dedicated VLESS + Reality + XHTTP service identities to the
foreign exits. WireGuard is not introduced.

The entry uses Xray observatory/balancer health to select among the route's
ordered foreign exits. Health changes may switch new flows; established flows
are not forcibly migrated. If every exit fails, the cascaded location fails
closed rather than sending traffic directly from the Russian entry and exposing
the entry IP as the final exit.

Cascade service identities are distinct from user/device UUIDs, have no
subscription URL, and are rotated through the same desired-state generation
protocol. Exit nodes apply normal abuse blocks and direct Internet egress after
the second hop. Logs remain disabled.

The user-facing location name makes the path explicit, for example
`Russia → Germany | Frankfurt`. Direct Germany remains a separate location.
Per-location VLESS/Hy2 selection continues to apply only to client-facing direct
locations until a future client-facing UDP cascade is explicitly designed.

### Cascade testing and activation

Automated tests cover schema migration, v1/v2 rolling compatibility,
entry/exit authorization boundaries, config generation, multiple-exit
selection, fail-closed behavior, route revocation while a node is offline, and
subscription suppression before all required acknowledgements.

Without a Russian VPS, integration uses network namespaces/containers to
simulate entry and two exits with latency and loss. Production activation
requires a separately enrolled Russian node, a canary-only route, explicit
latency/loss/throughput measurements, and confirmation that the node's hosting
terms permit the service.

## Release and Rollback

All workstreams land in one release cycle but not one irreversible deployment:

1. deploy Workstream 1 to Poland; verify the site, API, subscription headers,
   login, documents, and non-mutating checkout preparation;
2. deploy Workstream 2 to the USA Hysteria2 canary, then the remaining nodes;
3. build the PostgreSQL/Patroni standby, migrate a verified copy of SQLite,
   rehearse promotion, then schedule the brief central write cutover;
4. enable DNS failover only after both control hosts and all node control URLs
   pass;
5. deploy cascade schema v2 dormant, verify rolling compatibility, and keep
   every route disabled until a Russian entry exists.

Every stage has a commit boundary and rollback command. Website rollback
restores the prior static bundle and Caddyfile. Node rollback restores the
previous pinned image and keeps VLESS running. Database rollback is permitted
only before new writes are accepted on PostgreSQL; after cutover, rollback means
restoring PostgreSQL service, not writing back into stale SQLite. Cascade v2
rollback disables all routes and continues serving direct v1-compatible
locations.

## Acceptance Criteria

- Existing subscription URLs and user-selected per-location protocols remain
  valid.
- Subscription clients show the support bot and website info actions.
- The privacy text matches all persisted user profile fields and all users are
  prompted for the new document version.
- Public sequential avatar access is gone; private/account responses are
  non-cacheable.
- The site passes production build, automated accessibility checks, desktop and
  mobile browser smoke tests, and the specified security-header checks.
- No production build resolves an unpinned `latest` dependency or accepts an
  unverified Xray archive.
- Every Hysteria2 node installs renewed CA-signed certificates automatically
  before expiry without restarting Xray.
- Loss of Poland promotes USA without a second writable database; node control,
  subscriptions, website, payments, and Telegram singleton workers recover
  within the RPO/RTO targets.
- Direct VPN data planes continue operating through a complete central outage.
- Cascade schema v2 is deployed but advertises no Russian cascade until an
  enrolled entry node and healthy exits acknowledge the route.
- The complete Agent, bot, deploy, support-bot, and frontend test matrices pass
  before production rollout.
