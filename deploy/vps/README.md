# VPS Deploy

This deploy path is the current production layout for the main Aegis VPN VPS.

## Services

The control host uses `docker-compose.yml`:

- `xray` and `agent` - independently restartable data and control planes
- `bot`, `siteapi`, and `support-bot` - Telegram and public API services
- `caddy` - HTTPS reverse proxy and website
- `hysteria` - optional Hysteria 2 data plane

Remote VPN nodes use `docker-compose.node.yml`. It contains only `xray`,
`agent`, and the optional `hysteria` profile, so node updates never require
control-host-only `bot.env` or `support.env` files.

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
https://your-domain:8443
```

So the user-facing subscription URL becomes:

```text
https://your-domain:8443/sub/<token>
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
- `BOOTSTRAP_SERVER_NAME`
- `BOOTSTRAP_SERVER_FLAG`
- `BOOTSTRAP_PLANS_JSON`

3. Fill `vpn.env`:

- `XRAY_RUN_MODE=internal`
- `XRAY_CONFIG_PATH=/data/xray-config.json`
- `XRAY_PORT`
- `REALITY_DEST`
- `REALITY_SERVER_NAME`
- `HOST_IP`

4. Adjust [Caddyfile](C:/Users/detko/Documents/VPN/deploy/vps/Caddyfile) if your domain or port differs.

5. Start the control-host stack:

```bash
docker compose up -d --build
```

On a remote VPN node, use:

```bash
docker compose -f docker-compose.node.yml up -d --build
```

## Notes

- the bot usually binds to `127.0.0.1:8080`
- Caddy usually exposes HTTPS on `:8443`
- Xray port is configured in `vpn.env`
- if `443` is already occupied by another service, keep Xray on another port such as `9443`
- `PUBLIC_BASE_URL` must match the real public subscription URL seen by clients

## Adding More VPN Servers

Use [add_server.py](C:/Users/detko/Documents/VPN/deploy/vps/add_server.py) from the project root to provision and register additional VPN nodes.
