# Gemini access controls

Admin bot: Servers → location → **Доступ к Gemini**. The panel has
**Доступ: WARP / DNS / Decodo**, one setting for the selected method, and ACK
status. Changes save immediately; there is no draft or Apply button.
Opening/cancelling the panel does not change the node.

- **WARP**: node-wide; IPv4 or IPv6 via a `freedom` adapter (`gemini`) dialling
  the existing `warp` WireGuard outbound. No WARP key/address rotation;
  Microsoft remains on its IPv4 adapter.
- **DNS**: node-wide; enter a DoH `https://…/dns-query` address. A missing
  address prompts for input without changing the active policy. The resolver
  must provide Gemini unblocking: normal encrypted DNS alone does not.
  Existing DoT policies remain supported in the backend; new UI input is DoH.
- **Decodo (только для меня)**: experimental override for the sole configured
  administrator's active subscription/device identities, not a replacement for
  the location's normal DNS/WARP policy. Other users keep their existing route.
  Initially provisioned only on Stockholm (server 33). Select the previous
  DNS/WARP method to remove the override. That method is still node-wide.

DNS/WARP settings are remembered separately in `servers.gemini_preferences`.
Concurrent stale forms are rejected. Opening the panel never selects a default
policy. `servers.gemini_policy = NULL` preserves the legacy node policy,
including any pre-existing authenticated Gemini relay.

## Apply and compatibility

`gemini_policy` desired items require `gemini-policy-v1`; WARP selection also
requires `gemini-warp-v1`. Disabled nodes and stale forms cannot save policies.
The agent validates a complete candidate with its installed Xray binary,
persists policy in `/data/node-control/gemini-policy.json`, reloads Xray,
waits for readiness, then ACKs. Runtime failure restores config and policy.
Startup reapplies persisted policy after legacy environment helpers.

DoH uses `https+local://` (local fetch, no routed/plaintext resolver fallback).
Xray v26.7.28 does not implement native DoT: the retained DoT backend uses a
loopback DNS/TCP → TLS bridge on 127.0.0.1:18553 with CA/hostname verification,
bounded queries and no plaintext fallback. Gemini-specific DNS rules use
`disableFallbackIfMatch`; existing domain catalog and QUIC blocks are preserved.

ACK proves configuration application, **not Gemini country acceptance**.
Changing an egress policy reloads Xray and may briefly reconnect node clients.

## Admin-only Decodo trial

This is a free **shared datacenter** proxy, not a dedicated residential/ISP-IP
product. The observed trial allowance is 1 GB/month. No access guarantee is
made for Google; the operator tests actual application behavior.

- Node-local secret: `/data/node-control/gemini-admin-proxy.json` (0600), containing
  `host`, default `port`, `username`, `password`. The gateway is fixed to `dc.decodo.com`.
  The [Decodo free plan](https://help.decodo.com/docs/free-proxy-advanced-parameters) maps
  ten fixed IPs to ports `10001`–`10010`. Admin chooses a numbered slot; the button
  displays the port, not an unverified public exit address. No new credentials are required.
- Connection to the proxy is HTTP CONNECT over verified TLS. Neither
  `allowInsecure` nor plaintext proxy authentication is used.
- `gemini-admin-proxy-v1` and `gemini-admin-proxy-ip-v1` require a valid local profile.
  The control item `gemini_admin_proxy` contains internal client emails and, for
  a non-default selection on a capable agent, the bounded slot port. It never
  contains proxy credentials, Telegram IDs or a configurable proxy host.
  The default slot without a port field preserves legacy snapshot digests.
- `servers.gemini_admin_proxy` selects this mode. Control resolves the sole
  configured admin to active authorized subscriptions/devices. With multiple
  admins the UI service refuses activation; snapshots grant no identities.
- Agent intersects allowed emails with non-expired snapshot clients; it also
  covers the corresponding `_cdn` inbound emails. Rules require both a matching
  authenticated user and a Gemini catalog domain, allow proxying only TCP 443,
  and block matching UDP. Private-network/management guards stay ahead of the
  override; ordinary Gemini rules remain intact for everyone else.
- No proxy-outbound fallback to native/WARP on errors. Failure does not replace
  or remove the other users' normal Gemini routing.
- The last chosen slot is stored in `gemini_preferences.decodo_port` without a
  schema change. Emails and selected port persist in `/data/node-control/gemini-admin-users.json`
  and participate in the same validation/apply/rollback flow as global policy.
  Changing slots reloads Xray for that node; no external reachability test is implied.
  Removing the desired grant removes both managed rules and the proxy outbound.

Credentials never enter client subscriptions or source control. Backup files
containing runtime configs/proxy credentials must remain root-only. Do not roll
back to an agent that cannot parse active `gemini_admin_proxy` items: disable
that mode and wait for ACK first, or deploy a compatible rollback build.

## 2026-11-17 rollout

- Stockholm agent upgraded without restarting the Xray container. Four combined
  global-policy/admin-proxy candidates passed the installed Xray's config test.
- Proxy TLS certificate verified from Stockholm; **no CONNECT request or Gemini
  request through the proxy** was made by the assistant.
- Bot and siteapi updated together; no policy/Decodo selection made by deployment.
- 254 unique Stockholm client identities preserved. All 35 active subscriptions
  retained both Stockholm CDN fallbacks and had no standalone WL entries or
  proxy credentials.
- Protected backups: `/root/aegis/backups/gemini-decodo-20261117T174311Z` on
  Stockholm, `/root/aegis/backups/cdn-gemini-ux-20261117T174437Z` on control,
  including a root-only off-node archive of the Stockholm backup.
