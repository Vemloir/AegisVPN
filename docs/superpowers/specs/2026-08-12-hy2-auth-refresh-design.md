# Hysteria2 Auth Refresh During Pull Reconciliation

Date: 2026-08-12

## Goal

Make a newly created or restored device usable through Hysteria2 immediately,
without restarting the node agent or waiting for an unrelated client removal.

## Root Cause

The node agent keeps Hysteria2 credentials in an in-memory `uuid -> email` map.
Legacy push handlers refresh this map after writing the Xray client config, but
the authoritative pull/apply reconciliation path refreshes it only while
processing pending revocations.

An add-only snapshot therefore produces inconsistent live state:

- the device UUID is persisted in the Xray config;
- the UUID is added to the live VLESS inbounds;
- the applied control generation advances;
- the Hysteria2 auth map still contains the previous generation.

Hysteria2 then rejects the new UUID. Some clients surface the first failed
connectivity or DNS health check as `DNS probe timeout`, even though DNS is not
the source of the failure. An older device can continue working on the same
node, Wi-Fi network, and Xray version because its UUID was already loaded when
the agent started.

## Chosen Design

Whenever apply reconciliation persists a changed Xray configuration, rebuild
the Hysteria2 auth map immediately from that exact in-memory configuration.
The refresh occurs after the durable config write and before live Xray API
changes or the validated reload fallback.

This matches the ordering already used by `/client/add`, `/client/bulk`, and
`/client/remove`:

1. persist the authoritative client set;
2. update Hysteria2 authentication from the same client set;
3. apply the Xray live delta or reload fallback;
4. kick sessions whose identities were removed;
5. persist the applied control generation.

The same refresh covers additions and removals. For removals, refreshing before
the kick prevents the client from re-authenticating during the operation. A
cascade-only config change may perform a harmless refresh; no separate change
classifier is needed.

Reading the Xray file for every Hysteria2 authentication request is rejected
because it adds disk I/O and synchronization work to every connection. Agent
restarts and repeated push operations are rejected as temporary workarounds.

## Scope

- Change only the agent pull/apply reconciliation path.
- Do not change subscription URLs, device UUIDs, DNS policy, Xray JSON output,
  Hysteria2 server settings, TLS certificates, or protocol defaults.
- Do not query or mutate user records during diagnosis or verification.
- Preserve the existing live Xray API and reload fallback behavior.

## Testing

Add an add-only reconciliation regression test whose initial config and desired
snapshot differ only by one new device. It must prove that:

- the Hysteria2 refresh receives the new persisted configuration;
- the new UUID is present in that configuration;
- no Hysteria2 kick occurs;
- the applied generation advances;
- repeating the identical snapshot is idempotent and performs no refresh.

Run the complete agent test suite and lint checks. Existing mixed add/remove,
revocation retry, observe-mode, live-API fallback, and cached-snapshot tests must
remain green.

## Rollout and Verification

Deploy the agent to Germany and Switzerland as canaries without restarting the
Xray data-plane container. Recreate only the agent container, which loads the
current client set into the Hysteria2 auth map during startup.

For each canary:

- verify agent and Hysteria2 container health;
- verify UDP/443 and the expected certificate/SNI;
- publish or apply a synthetic client through the normal control path;
- confirm that Hysteria2 reaches the auth endpoint without an unknown-client
  rejection;
- remove the synthetic client through the same path;
- verify existing VLESS service remains healthy.

After both canaries pass, deploy the agent change to the remaining nodes. Users
do not need to re-import or manually refresh their subscription; an automatic
subscription fetch continues using the same device UUID and profile.
