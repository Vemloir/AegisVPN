# Multi-Protocol Locations: VLESS, Trojan, and Shadowsocks 2022

Date: 2026-07-29

## Objective

Offer three user-selectable protocols for each accessible VPN location:

- VLESS
- Trojan
- Shadowsocks 2022

VLESS remains the default. Hysteria2 is removed from the user-facing menu and
existing Hy2 preferences are reset to VLESS. WireGuard is out of scope.

Protocol selection must preserve:

- the existing single subscription URL;
- unlimited devices;
- restricted/dedicated locations used to provide a permanent public IP;
- device suspension, removal, traffic accounting, and connection limits;
- existing subscriptions without requiring users to import them again.

## Decisions

### Recommended architecture

Use the existing Xray process for all three protocols.

- VLESS continues to use the current REALITY inbounds.
- Trojan uses a dedicated Xray TLS/TCP inbound.
- Shadowsocks uses an Xray Shadowsocks 2022 TCP+UDP inbound.
- The agent renders all enabled inbounds from one authoritative client set and
  continues to update clients through the Xray API where supported, with a
  validated configuration reload as fallback.

This keeps authentication, statistics, lifecycle management, and deployment in
the existing data plane. Separate Trojan and Shadowsocks daemons were rejected
because they would duplicate authentication, reconciliation, logging, and
traffic accounting. A protocol-specific subscription URL was also rejected
because it would break the existing one-URL, per-location settings model.

### Protocol and port allocation

The initial allocation is:

| Protocol | Listener | Purpose |
| --- | --- | --- |
| VLESS REALITY XHTTP | existing TCP/443 | Existing optional VLESS transport |
| VLESS REALITY Vision | existing TCP/2053 | Default VLESS transport |
| Trojan TLS | TCP/2087 | TLS/TCP alternative without displacing VLESS |
| Shadowsocks 2022 | TCP+UDP/8448 | General TCP/UDP alternative |

Provisioning must test that each requested port is free before changing the
node. A conflict fails the deployment before Xray is reloaded.

Trojan cannot use TCP/443 without removing the existing VLESS XHTTP listener.
The existing transport remains available, so Trojan receives its own common TLS
alternative port.

### Hysteria2 retirement

- Remove Hy2 from the protocol chooser and translations.
- Convert every stored `protocol=hy2` preference to the VLESS default.
- Stop and disable the Hysteria container on deployed nodes.
- Close its public UDP listener after the canary succeeds.
- Keep the Hy2 source code and database fields dormant for possible later work.
- Do not emit `hysteria2://` links or Xray Hysteria outbounds.

No Hysteria authentication secret or diagnostic artifact remains active after
the rollout.

## Public and Internal Addresses

A permanent IP is a public exit-node address, not a client tunnel address.

Restricted servers and `server_access_grants` continue to determine which users
can see and use a dedicated location. Switching that location from VLESS to
Trojan or Shadowsocks does not change its public exit IP. All devices assigned
to the same restricted node still appear on the internet as that node's public
address.

Neither Trojan nor Shadowsocks requires a private per-device tunnel address.

## Capabilities and Database State

Add server capabilities for the two new protocols:

- `trojan_enabled`
- `trojan_port`
- `trojan_sni`
- `ss2022_enabled`
- `ss2022_port`
- `ss2022_method`
- `ss2022_server_password`

Capability predicates require every field necessary to construct a valid
profile. A partially provisioned protocol is never offered and resolves to
VLESS if stale preference data refers to it.

The allowed preference values become:

- `vless`
- `trojan`
- `shadowsocks`

The default is `vless` with the TCP/Vision transport. Existing explicit VLESS
transport choices remain unchanged. Hy2 preference rows are migrated to the
VLESS default rather than left as stale values.

Server-side Shadowsocks master passwords are generated with cryptographically
secure randomness during provisioning and are never logged. Database backups
must be protected because the current database already stores operational
secrets and the Shadowsocks master password joins that trust boundary.

## Device Credentials

Every active device remains an independent identity.

### VLESS

Continue using the device UUID as the VLESS user ID.

### Trojan

Use the device UUID as the Trojan password. UUIDs are already random,
device-specific bearer credentials distributed to the same trusted
subscription response. Reusing the identity avoids a new secret lifecycle while
preserving independent suspension and removal.

### Shadowsocks 2022

Use `2022-blake3-aes-256-gcm`.

Each node has a 32-byte server master key. Its device subkey is derived as:

```text
base64(HMAC-SHA256(base64_decode(server_master_key), device_uuid))
```

The client password is the SS2022 multi-user composite:

```text
server_master_key:device_subkey
```

The derivation is deterministic, so the bot and agent do not need another
per-device secret table. Device removal removes the derived user from the
inbound. Rotating the node master key invalidates all old Shadowsocks profiles
for that node and requires an ordinary subscription refresh, not a re-import.

Unlimited devices remain supported because no credential is shared between
devices.

## Node Configuration and Control Plane

The desired-state client record continues to carry the device UUID and unique
email. The agent expands each record into users for all enabled protocol
inbounds:

- VLESS user: UUID, email, Vision flow where applicable;
- Trojan user: UUID password, email;
- Shadowsocks user: derived subkey, email.

The agent must treat the rendered configuration as atomic:

1. render a candidate;
2. run Xray configuration validation;
3. replace the active configuration only after validation;
4. apply live API changes where Xray supports them;
5. use a controlled reload only when live mutation cannot represent the change;
6. retain the previous valid configuration on any failure.

The pull control plane remains authoritative. Legacy push endpoints continue to
work during the canary and receive the same UUID/email inputs.

Traffic and online accounting must aggregate the same email across VLESS,
Trojan, and Shadowsocks inbounds without double-counting. Suspension,
subscription expiry, access revocation, and device deletion remove the identity
from every enabled inbound in the same reconciliation generation.

## Subscription Output

Protocol selection stays per user and per location. The selected protocol
changes only that location's emitted profile.

### Xray JSON clients

Generate a complete standalone Xray configuration for each location:

- VLESS outbound for `vless`;
- Trojan outbound with TLS, SNI, certificate validation, and Mux for `trojan`;
- Shadowsocks outbound with the SS2022 composite password for `shadowsocks`.

Existing DNS, routing, private-address, RU/CN direct-routing, and local inbound
policies remain common to all three.

### Link-list clients

Emit standard share URIs:

- `vless://`
- `trojan://`
- `ss://`

No Hy2 or WireGuard URI is emitted.

If a stored selection is not supported by the node, subscription generation
falls back to VLESS for that location and never emits a knowingly broken
profile.

Subscription URLs and metadata headers do not change. Existing clients receive
the new list on their next update.

## Bot User Experience

The location protocol menu contains:

1. VLESS
2. Trojan
3. Shadowsocks

Only capable protocols are shown. A protocol is absent from a location until
that node acknowledges the corresponding applied capability.

The transport selector is visible only for VLESS. Trojan and Shadowsocks have
no second transport screen.

Russian and English labels and error messages must be complete. Changing a
protocol updates the existing preference and does not regenerate the
subscription token.

## TLS and Security

Trojan uses a publicly trusted certificate and validates the configured SNI.
Certificate distribution and renewal are generalized from the existing Hy2
certificate path so they remain active after Hysteria is disabled.

Trojan runs with client-side Mux enabled as required for its public TLS usage by
current Xray guidance. Its inbound has an explicit safe fallback/decoy rather
than exposing a protocol-specific failure to unauthenticated probes.

Shadowsocks uses only the 2022 cipher suite. Legacy Shadowsocks AEAD methods are
not offered. Shadowsocks and Trojan are alternatives, not automatic fallback
chains, so failures are explicit and do not silently route a selected location
through another public node.

Secrets, UUIDs, and generated links must be redacted from deployment and test
output.

## Failure Handling

- Missing capability fields: resolve the location to VLESS.
- Invalid node candidate config: keep the previous config and report the node
  generation as failed.
- Certificate unavailable or expired: disable Trojan capability; do not emit an
  insecure profile.
- Shadowsocks master key unavailable: disable Shadowsocks capability.
- Node cannot apply a generation: keep the previous advertised capabilities
  until it acknowledges the new generation.
- Client selects a protocol during a rollout race: emit VLESS unless the node's
  applied capability is acknowledged.

## Testing

### Unit and integration tests

- capability predicates for complete and partial configurations;
- protocol preference validation and Hy2 migration;
- VLESS default remains TCP/Vision;
- Trojan URI and Xray JSON shape;
- Shadowsocks 2022 URI, composite password, and deterministic subkey;
- standard-link and JSON content negotiation;
- dedicated/restricted location visibility for every protocol;
- device creation, suspension, resumption, deletion, and unlimited-device
  behavior;
- subscription expiry and access revocation remove all protocol identities;
- traffic aggregation does not double-count;
- no Hy2 entry appears in menus or subscriptions.

Every rendered server and client configuration is tested with the pinned Xray
binary using its configuration validation mode.

### Live canary

Deploy to one non-main exit node first and verify:

- VLESS TCP/Vision regression;
- Trojan TCP connection and public exit IP;
- Shadowsocks TCP and UDP;
- two devices connected concurrently with independent credentials;
- device suspension terminates and blocks reconnect;
- restricted-node user exits through the same permanent public IP on all three
  protocols;
- subscription update requires no re-import;
- restart and certificate renewal preserve all inbounds.

After the canary observation period, roll out node-by-node. The bot exposes a
new protocol for a node only after that node acknowledges its applied
capability.

## Non-Goals

- WireGuard;
- automatic protocol selection or probing;
- reimplementing Hysteria2;
- separate protocol-specific subscription URLs;
- changing plans, pricing, restricted-server grants, or the meaning of a
  permanent IP;
- changing the public subscription token or device limit policy.
