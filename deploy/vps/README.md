# VPS Deploy

This deploy path is the current production layout for the main Aegis VPN VPS.

## Services

The control host uses `docker-compose.yml`:

- `bot`, `siteapi`, and `support-bot` - Telegram and public API services
- `caddy` - HTTPS reverse proxy and website; the sole TCP/443 listener
- `xray` and `agent` - optional local data plane behind the `local-exit` profile
- `hysteria` - optional Hysteria 2 data plane

Nodes whose provider path filters or black-holes QUIC on UDP/443 can expose
UDP/2053 without changing Hysteria's internal listener. Install and enable
`systemd/aegis-hy2-alt-port.service`, open `2053/udp` in the host firewall, and
set that server's `hy2_port` to `2053` in the control-plane database. UDP/443
continues to work, so this can be enabled per node without affecting others.

The production control host is not a VPN exit. A default `docker compose up`
therefore excludes `xray` and `agent`, preventing Linux `SO_REUSEPORT` from
splitting HTTPS connections between Caddy and Reality.

Remote VPN nodes use `docker-compose.node.yml`. It contains `xray`, `agent`,
and the optional `hysteria` and `mtproxy` profiles, so node updates never
require control-host-only `bot.env` or `support.env` files. MTProxy reads its
secret from root-only `data/mtproxy/config.toml`, not process arguments.

## What This Deploy Does

- runs the Telegram bot in polling or webhook mode
- stores data in SQLite by default
- bootstraps the primary VPN server from `/vpn-data/agent.env`
- enforces a single `30-day` plan at startup
- exposes the subscription endpoint through HTTPS
- hides `/` and `/info.json` with `404`

## Public Endpoints

- `GET /health`
- `GET /sub/<token>`

Typical public base URL:

```text
https://your-domain
```

So the user-facing subscription URL becomes:

```text
https://your-domain/sub/<token>
```

## Setup

1. Copy example env files:

```bash
cd deploy/vps
cp bot.env.example bot.env
cp vpn.env.example vpn.env
mkdir -p data/bot data/vpn data/caddy config/caddy
```

2. Fill `bot.env`:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `PUBLIC_BASE_URL`
- `BOOTSTRAP_PLANS_JSON`

Set `BOOTSTRAP_SERVER_ENABLED=true` plus `BOOTSTRAP_SERVER_NAME` and
`BOOTSTRAP_SERVER_FLAG` only when this control host intentionally runs a
co-located VPN exit. Leave it false on control-only hosts; otherwise a stale
`data/vpn/agent.env` can be registered as a duplicate location.

3. Fill `vpn.env` only if this host is intentionally also a VPN exit. Its
   `XRAY_PORT` must not conflict with Caddy TCP/443.

4. Adjust [Caddyfile](C:/Users/detko/Documents/VPN/deploy/vps/Caddyfile) if your domain or port differs.

5. Start the control-host stack:

```bash
docker compose up -d --build
```

On a remote VPN node, use:

```bash
docker compose -f docker-compose.node.yml up -d --build
```

To deliberately enable a local VPN exit, target the profiled services and use
a non-conflicting port:

```bash
docker compose --profile local-exit up -d --build xray agent
```

## Notes

- the bot usually binds to `127.0.0.1:8080`
- Caddy exposes the website and subscriptions on TCP/443
- legacy subscription URLs may remain available on TCP/8443
- a local Xray port is configured in `vpn.env` and must not be TCP/443
- `SUBSCRIPTION_PUBLIC_BASE_URL` must match the URL seen by clients

## Adding More VPN Servers

Use [add_server.py](add_server.py) from the project root to provision and
register a fresh VPN node. By default it configures both VLESS transports,
registers and applies a unique WARP identity, sets `GEMINI_EGRESS=warp` and
`WARP_DOMAIN_STRATEGY=ForceIPv6` for Gemini and Microsoft through WARP IPv6,
and starts a pinned,
Compose-managed MTProxy on TCP/80. Games and ordinary unmatched traffic keep
the node's direct egress. WARP registration or MTProxy startup
failures stop provisioning; `--no-warp` and `--no-mtproxy` are explicit opt-outs.
Hysteria2 remains a separate per-node opt-in, not a prerequisite for VLESS.

Do not rerun the fresh-node bootstrap on an existing node: it resets client
state and observation settings. For a partial installation, back up the node
and database, preserve its Reality/control identities and current control
mode, then complete only the missing components. For a fresh rollout, pass `--hidden-from-subscription` to stage the new
location without advertising it; publish it only after verification. Before
advertising success, verify real WARP egress (`warp=on`), ordinary direct egress, both VLESS
transports, MTProxy health, current exact control ACK, and existing-device
subscription output. Keep private-network blocking and game traffic outside
WARP. Record explicitly whether optional Hysteria2 is enabled.
