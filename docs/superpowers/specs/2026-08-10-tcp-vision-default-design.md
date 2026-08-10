# TCP+Vision Default Transport

## Goal

Make VLESS over TCP+REALITY with `xtls-rprx-vision` the default transport for every location that exposes a TCP port. Keep XHTTP available as an explicit per-location choice.

## Behaviour

- A user without a stored preference receives TCP+Vision on TCP-capable locations.
- Selecting TCP+Vision removes the preference row because it is the canonical default.
- Selecting XHTTP stores an explicit `vless/xhttp` preference and continues to emit XHTTP.
- A location without a TCP capability falls back to XHTTP, both in subscription output and in the settings UI.
- Existing users with no preference rows move to TCP+Vision on their next subscription refresh. Under the old semantics, choosing XHTTP deleted the row, so that historical choice cannot be distinguished from never choosing anything; those users also move to TCP and can explicitly select XHTTP again. Any actual stored XHTTP row remains XHTTP.
- No UUID, Reality key, subscription URL, server port, or access assignment changes.

## Data flow

`SubscriptionService` resolves a concrete default per server: TCP when `tcp_port` exists, otherwise XHTTP. The preference mapper must include this resolved TCP override even when the database has no row, because the node's safe subscription template is XHTTP. Link normalization then retargets the link to `tcp_port`, sets `network=tcp`, removes XHTTP-only parameters, and adds `flow=xtls-rprx-vision`.

The model default and UI ordering are updated to match the product default. XHTTP remains selectable and stored explicitly.

## Compatibility

The change is bot-only. Data-plane containers do not restart. Clients receive the new transport when their subscription refreshes automatically or manually. XHTTP-only and temporarily misconfigured nodes remain usable through capability-aware fallback.

## Verification

- Missing preference on a TCP-capable server produces TCP+Vision and the TCP port.
- Missing preference on an XHTTP-only server produces XHTTP.
- Explicit XHTTP survives subscription generation.
- Selecting the TCP default deletes a redundant row; selecting XHTTP creates one.
- Existing subscription, location-keyboard, and bot test suites remain green.
