# Outbound node control deployment

The control plane uses ordinary HTTPS on TCP/443. Nodes initiate every
connection, authenticate with a unique mTLS certificate plus a unique bearer
token, and fetch complete immutable desired-state snapshots. WireGuard and
public inbound management ports are not part of this design.

The device count is intentionally unlimited. A snapshot is paginated to bound
each HTTP request; pagination is not a device limit.

## 1. Initialize the central endpoint

Choose an operator-only directory outside the repository for the CA private
key. The second directory is staging material for the control host:

```bash
python3 deploy/vps/control_plane.py init-central \
  --ca-dir /secure/operator/aegis-control-ca \
  --server-output /secure/operator/aegis-control-server
```

This creates:

- `client-ca.key` only in the operator CA directory;
- `client-ca.crt`, `proxy-secret`, and `control.caddy` in the server staging
  directory;
- mode `0600` for the CA key and proxy secret.

Copy the three server files to
`/root/aegis/deploy/vps/data/control/server/` on the central host, set
`CONTROL_DOMAIN=control.example.com` in the Compose environment, point that DNS
name at the central host, and recreate only Caddy, `siteapi`, and `bot`.
The main `Caddyfile` imports `control.caddy` only when it exists.

Before enrollment, verify:

```bash
curl https://control.example.com/api/node/v1/sync
```

The request must fail because no client certificate was presented. Caddy
removes any caller-supplied identity headers before injecting the verified
certificate fingerprint and loopback-only proxy secret.

## 2. Enroll a new node in observation mode

`add_server.py` creates one certificate/key/token, uploads the private files
with mode `0600`, and stores only the control-token SHA-256 digest and
certificate fingerprint in the bot database:

```bash
python3 deploy/vps/add_server.py \
  --main-host 203.0.113.10 \
  --main-password '...' \
  --new-host 198.51.100.20 \
  --new-password '...' \
  --server-name '🇫🇮 Finland | Helsinki' \
  --server-domain 198.51.100.20 \
  --country-code FI \
  --reality-dest example.org:443 \
  --reality-server-name example.org \
  --control-url https://control.example.com \
  --control-ca-dir /secure/operator/aegis-control-ca
```

The fixed `--main-host` IPv4 address is the only source permitted to reach the
temporary Agent TCP/8444 endpoint. Xray TCP/443 and Hysteria UDP ports are not
changed by that firewall rule. Observation downloads and verifies snapshots but
does not mutate or acknowledge them; the existing push path stays active.

## 3. Promote one canary

Promotion first changes the node reconciler to authoritative `apply`, waits for
a fresh acknowledgement whose generation and digest exactly match the central
snapshot, and only then binds Agent to loopback, drops public TCP/8444, and
sets the database node mode to `pull`:

```bash
python3 deploy/vps/update.py \
  --promote-pull \
  --server-id 7 \
  --main-host 203.0.113.10 \
  --main-password '...' \
  --node 198.51.100.20:'...'
```

Only the Agent container is recreated. Xray is not restarted, so current VPN
sessions remain alive. Roll out one node at a time and confirm fresh
`control_last_seen_at`, an empty `control_last_error`, and equal
desired/applied generation and digest before moving to the next node.

## 4. Roll back without exposing management publicly

```bash
python3 deploy/vps/update.py \
  --rollback-observe \
  --server-id 7 \
  --main-host 203.0.113.10 \
  --main-password '...' \
  --node 198.51.100.20:'...'
```

Rollback is idempotent. It restores `observe` plus the legacy push path, but
TCP/8444 remains reachable only from the fixed central-host IP.

## 5. Rotate or revoke credentials

Issue replacement material without printing the token or private key:

```bash
python3 deploy/vps/control_plane.py issue-node \
  --ca-dir /secure/operator/aegis-control-ca \
  --node-output /secure/operator/rotations/node-7 \
  --node-name node-7
```

The command prints only the token hash, certificate fingerprint, and output
directory. Install all four generated files on the node atomically, update the
stored hash/fingerprint in one database transaction, then recreate only Agent.
Keep the old material until the new heartbeat arrives; remove it immediately
afterward.

For emergency deactivation set `servers.is_active=0`. The node may still
authenticate long enough to receive an empty desired snapshot and drain every
client, but it is excluded from new subscriptions. If credentials are suspected
compromised, also clear `control_token_hash` and
`control_cert_fingerprint`; this rejects the node at the next request.

## 6. Endpoint failure and recovery

Supply `--control-url` more than once during enrollment to configure ordered
HTTPS failover. The agent remembers the last successful endpoint, uses bounded
exponential backoff, and reconciles its cached snapshot locally while all
endpoints are unavailable so expired access is still removed. When connectivity
returns it downloads the latest complete snapshot; changes made during the
outage do not need to be replayed individually.

Never commit or log CA keys, client keys, node tokens, proxy secrets, UUID
lists, or generated credential directories.
