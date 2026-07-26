# Node control operations

This runbook covers the outbound AegisVPN node-control channel. The detailed
copy paths and enrollment commands are in
[`deploy/vps/control-plane/README.md`](../deploy/vps/control-plane/README.md).

## Invariants

- Nodes initiate HTTPS connections on standard TCP/443; WireGuard is not used.
- Caddy verifies a per-node client certificate and injects identity headers only
  on the loopback upstream request.
- The bot database is the source of truth.
- A node acknowledges only a complete snapshot whose page and final SHA-256
  digests match and whose local reconciliation completed.
- Normal add/remove operations use the live Xray API. Agent deployment and
  pull promotion never restart Xray.
- There is no device-count limit. Pagination bounds individual requests.

## Rollout state

```text
push
  └─ enroll credentials + fixed-IP firewall
       └─ observe (download/compare; legacy push still active)
            └─ apply (exact reconcile; wait for ACK)
                 └─ pull (Agent loopback; TCP/8444 closed)
```

Promote one node at a time:

```bash
python3 deploy/vps/update.py \
  --promote-pull \
  --server-id SERVER_ID \
  --main-host FIXED_CONTROL_IPV4 \
  --main-password '...' \
  --node NODE_IPV4:'...'
```

The guard requires:

- `desired_generation == applied_generation`;
- the applied digest equals the desired snapshot digest;
- `control_last_seen_at` is no older than 90 seconds;
- `control_last_error` is empty.

If any check fails, the central mode stays `observe` and TCP/8444 remains
allowlisted only to the fixed central IP.

## Monitoring

Alert on:

- heartbeat older than 90 seconds for a pull node;
- non-empty `control_last_error`;
- desired/applied generation mismatch lasting more than two poll cycles;
- telemetry sequence that stops advancing;
- repeated control HTTP 401, 413, 5xx, digest, or schema errors;
- unexpected Xray fallback reloads.

Never include tokens, certificate material, UUIDs, full request bodies, or
subscription URLs in alerts.

## Recovery

When a node reconnects after an outage, it fetches the newest complete
generation. Individual missed changes are irrelevant: a revoke while offline
produces a snapshot without the base UUID and every device UUID, and exact
reconciliation removes them before acknowledgement. While still offline, the
cached snapshot's absolute expiries are reevaluated locally.

A corrupt local applied-state file is treated as generation zero, forcing a
fresh complete download. Atomic config/state writes preserve the previous file
if replacement is interrupted. A stale acknowledgement cannot lower the
recorded applied generation.

## Rollback

```bash
python3 deploy/vps/update.py \
  --rollback-observe \
  --server-id SERVER_ID \
  --main-host FIXED_CONTROL_IPV4 \
  --main-password '...' \
  --node NODE_IPV4:'...'
```

This is idempotent and does not restart Xray. It restores observe plus legacy
push, but never exposes Agent to arbitrary Internet sources.

## Credential incident

For routine rotation, use the bounded old/new credential-pair overlap described
in the deployment runbook. The server accepts only the complete old pair or the
complete new pair; mixed halves fail authentication.

For suspected compromise:

1. Set `is_active=0` to remove the location and publish an empty desired state.
2. If safe draining is still possible, wait for its empty-snapshot ACK.
3. Clear current and previous token hashes and certificate fingerprints to
   reject further requests.
4. Issue a new node identity or reprovision the host.
5. Rotate the Caddy proxy secret if the central host, rather than one node, may
   be compromised.

The CA private key stays off the control host. A CA compromise requires a new
CA, new certificate for every node, replacement of Caddy's trust pool, and
revocation of all old fingerprints/tokens.

## Control endpoint failover

Configure multiple `CONTROL_URLS` values, each on HTTPS/TCP 443 and backed by a
control service that reads the same authoritative database. The agent tries the
last healthy endpoint first, then fails over with bounded exponential backoff.
Do not put a generic public reverse proxy in front unless it preserves the same
mTLS client-certificate verification and trusted-header overwrite rules.

## PostgreSQL/Patroni control-plane failover

The dormant HA package lives in `deploy/vps/ha/`. Poland and USA are PostgreSQL
members; Poland, USA, and Germany form the three-vote etcd quorum. Applications
connect to the writable Patroni member only through local HAProxy on
`127.0.0.1:5433`. Telegram polling and every scheduler job use PostgreSQL
advisory locks, so a warm standby does not consume duplicate updates.

Before cutover:

1. generate a private HA CA plus per-host/client certificates outside Git;
2. start etcd on all three members, then Patroni on Poland and USA;
3. run `migrate_sqlite_to_postgres.py` against a copy of the SQLite database;
4. compare every table's row count and SHA-256 report;
5. stop the preferred member and use `verify_failover.py` to prove exactly one
   node is writable;
6. restore Poland and repeat before changing the live application DB URL.

Never write back to the old SQLite database after PostgreSQL accepts production
writes. Rollback after that point restores Patroni/PostgreSQL, not SQLite.

## Cloudflare DNS-only failover

Automatic DNS switching is disabled until a scoped token is installed. The
token may edit DNS records only in the AegisVPN zone. Cloudflare proxying
remains disabled and every managed record has TTL 60.

Copy `deploy/vps/ha/cloudflare.env.example` to a root-readable environment file.
Set only the exact apex, `www`, subscription, and control records that should
follow the active application host. The Patroni callback mutates DNS only when
the local node is primary and both Patroni and the application health endpoint
return success.

Dry verification uses the test fake and does not contact production:

```bash
PYTHONPATH=. bot/.venv/bin/pytest \
  deploy/vps/tests/test_cloudflare_failover.py -q
```

After an update, the previous values are atomically saved with mode 0600. Manual
rollback is:

```bash
set -a
. /etc/aegis-ha/cloudflare.env
set +a
python3 /opt/aegis-ha/cloudflare_failover.py \
  --restore /var/lib/aegis-ha/cloudflare-rollback.json
```

Do not echo the environment file or run the command with shell tracing. If no
scoped token is available, promote PostgreSQL manually and change the four
allowlisted DNS records in the Cloudflare dashboard; do not claim automatic
failover.
