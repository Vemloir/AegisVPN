# Agent Pull Control Plane

Date: 2026-07-26

## Objective

Replace the publicly reachable, bot-initiated Agent API with a node-initiated
control channel over ordinary HTTPS on TCP/443.

The design must:

- keep node management working when a node is behind NAT or an inbound firewall;
- avoid WireGuard and other easily classified management tunnels;
- converge after transient outages, including applying revocations missed while
  a node was offline;
- support future Russian entry nodes and cascaded VPN routes;
- preserve Aegis VPN's unlimited-device feature;
- avoid restarting Xray for normal client additions and removals.

Building the cascaded user data plane itself is outside this iteration. This
iteration establishes a control plane that can manage both entry and exit nodes
when cascade support is added later.

## Current Problem

The bot currently calls every node at a public URL such as
`http://<node>:8444` and authenticates with a bearer token. This exposes the
management service to the Internet and sends its bearer token and control
payloads without transport encryption.

The push model also treats a failed removal as a best-effort event. If a node is
offline during a revocation, it can retain an obsolete UUID indefinitely because
there is no authoritative reconciliation after it returns.

## Chosen Architecture

The control database on the main VPS is the source of truth. Each node runs an
outbound sync loop and connects to a dedicated control hostname over HTTPS on
TCP/443. No node accepts remote management connections.

The control hostname is separate from the public website hostname so Caddy can
require a client certificate during the TLS handshake without affecting normal
website visitors. Caddy terminates TLS, validates the node certificate against a
private control CA, replaces any client-supplied identity headers with the
validated certificate fingerprint, and proxies only to a loopback-bound control
API.

Every node has two independent credentials:

1. a unique mTLS client certificate;
2. a high-entropy node token whose digest, rather than plaintext value, is stored
   by the control service.

The certificate fingerprint and token are mapped to exactly one active server
record. A credential for one node cannot be used to read or acknowledge another
node's state.

The existing Agent HTTP app binds to loopback only. It remains available for
local Hysteria authentication and operator diagnostics, but port 8444 is closed
on public interfaces.

## Desired-State Protocol

Each server has a monotonically increasing `desired_generation`. The generation
is incremented in the same database transaction as any node-relevant change.
The first protocol version covers:

- authorized client UUID, email and expiry;
- per-user connection-limit overrides;
- the schema version and required agent capabilities.

Transport and cascade configuration can be added in a later version without
changing the identity or synchronization model.

The node performs these operations:

1. Submit its node identity, applied generation, applied digest, capabilities,
   agent version and health summary.
2. If its generation is current, hold the request as a bounded long poll. The
   server responds immediately when desired state changes, or returns a no-change
   response before intermediary timeouts.
3. When a newer generation exists, obtain an immutable snapshot manifest and
   download its bounded pages.
4. Verify the snapshot generation, page ordering and final digest.
5. Reconcile the complete desired set against local state.
6. Persist the resulting configuration atomically, apply live Xray changes,
   refresh Hysteria authorization, and kick removed Hysteria sessions.
7. Acknowledge the generation only after every required local step succeeds.

The protocol is idempotent. Repeating a page download, reconciliation or
acknowledgement produces the same result. A node never applies a generation
older than its last successfully applied generation.

Snapshots are paginated and content-addressed. There is no device-count limit;
pagination bounds memory, response size and per-request work without changing
the unlimited-device product behavior.

## Local Reconciliation and Failure Semantics

The agent reconciles a full snapshot rather than executing an arbitrary remote
command queue. Therefore a node returning after an outage removes every UUID
that is no longer authorized, even if it missed the original removal event.

The agent keeps the last successfully applied configuration while the control
service is unreachable. Explicit client expiries continue to be enforced from
the locally stored expiry time. This favors data-plane availability without
allowing a disconnected node to extend a known expiry.

Live Xray API calls remain the normal path. If a live update is only partially
successful, the existing controlled reload fallback may be used. The generation
is not acknowledged until the on-disk configuration and running authorization
state are consistent. Failed acknowledgements are safe to retry.

The sync loop uses bounded timeouts and exponential backoff with full jitter.
Nodes start with a randomized delay to prevent a thundering herd after a control
service restart. A configured list of control URLs may be tried in order, but
all endpoints must authenticate the same node identity and serve the same
generation history.

## Telemetry

Nodes send health, traffic counters, online identities, applied generation and
the last reconciliation result over the same authenticated outbound channel.
Telemetry is bounded and has sequence metadata so duplicate delivery is safe.

The control service records at least:

- last node contact;
- desired and applied generations;
- last successful reconciliation time;
- last error code and a redacted diagnostic;
- agent version and reported capabilities.

Operators must be able to distinguish an offline node from a reachable node that
cannot apply its desired state.

## Security Boundaries

- The node control API is reachable only through the dedicated mTLS virtual
  host on TCP/443.
- The backend trusts the certificate fingerprint header only from its
  loopback-bound Caddy proxy; Caddy deletes and recreates that header.
- Requests without a valid client certificate, active fingerprint mapping and
  correct node token are rejected.
- A node may access only its own snapshot and submit only its own telemetry and
  acknowledgements.
- Snapshot and telemetry payloads have explicit schema validation, page-size
  bounds and decompressed-size bounds.
- Node tokens and certificates can be rotated independently. Deactivating a
  server record immediately rejects both control access and acknowledgements.
- UUIDs, tokens, private keys and certificate material are never logged or
  committed to the public MIT-licensed repository.
- The public Agent port is closed only after the new channel has been verified
  for that node.

## Rollout

Migration is performed one node at a time without restarting the client data
plane:

1. Add the control API, state-generation persistence, mTLS virtual host and node
   credential provisioning.
2. Deploy an agent with outbound sync in observation mode. It downloads and
   validates snapshots but does not mutate Xray.
3. Compare its computed state with the existing node configuration and resolve
   any mismatch.
4. Enable reconciliation for one canary node while retaining the old push path.
5. Verify additions, removals, expiry, statistics, offline recovery and
   generation acknowledgements.
6. Make pull reconciliation authoritative for the canary and remove its public
   Agent URL from normal bot operations.
7. Bind its Agent API to loopback and close public TCP/8444.
8. Repeat for the remaining nodes.
9. Remove remote push calls and plaintext agent tokens from the central server
   records after every active node has completed migration.

Rollback before step 7 consists of disabling reconciliation and retaining the
old push path. After step 7, rollback re-enables the old port only for the fixed
control-server IP and only for the time needed to restore pull operation.

## Verification

Automated tests cover:

- mTLS identity mapping, token verification and cross-node isolation;
- generation increments in the same transaction as desired-state changes;
- stable paginated snapshots and digest verification;
- duplicate delivery, duplicate acknowledgement and out-of-order generations;
- add, remove, expiry and connection-limit reconciliation;
- a revocation made while a node is offline and applied after reconnect;
- partial live-apply failure without a false acknowledgement;
- restart recovery from the last atomically persisted generation;
- spoofed proxy identity headers and unauthenticated requests;
- request, page and decompression bounds;
- backoff, jitter, long-poll timeout and control-URL failover.

The canary rollout additionally verifies that normal subscription issuance,
Xray live updates, Hysteria authentication, statistics collection and current
client sessions continue to work without an Xray restart.

## Acceptance Criteria

- No active node exposes Agent management endpoints on a public interface.
- All control and telemetry traffic uses outbound TLS on TCP/443 with per-node
  mTLS identity and token authentication.
- An offline node converges to current authorization state after reconnecting.
- The control service shows desired versus applied generation for every node.
- Normal add/remove operations remain idempotent and do not restart Xray unless
  the existing live-API fallback is required.
- Snapshot synchronization remains bounded per request without imposing a
  device-count limit.
- The migration can be performed node by node without interrupting existing VPN
  sessions.
