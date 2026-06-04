# Aegis VPN

**Telegram bot:** [@AegisEcoVPN_bot](https://t.me/AegisEcoVPN_bot)

Aegis VPN is a Telegram bot for selling and managing VPN subscriptions backed by Xray (`VLESS + Reality`) on one or more VPS nodes.

The project now uses a split architecture:

- `bot` stores users, subscriptions, plans, server access rules, and serves the subscription URL.
- `agent` runs on each VPN server, manages the local Xray config, and exposes a small HTTP API for add/remove client operations.
- `deploy/vps` contains the production compose stack for the main VPS.
- `Caddy` terminates HTTPS for the bot subscription endpoint and proxies traffic to the internal bot HTTP server.

## Current Architecture

### Main VPS

The main VPS usually runs three services from [deploy/vps/docker-compose.yml](deploy/vps/docker-compose.yml):

- `aegis-vpn` - local `agent + xray`
- `aegis-bot` - Telegram bot, SQLite DB, subscription endpoint
- `aegis-caddy` - HTTPS reverse proxy for the bot

Typical flow:

1. A user buys or renews a subscription in Telegram.
2. The bot creates or updates a subscription record in SQLite.
3. The bot syncs the user's UUID to all servers available to that user.
4. Each server `agent` writes the UUID into the local Xray config and reloads Xray.
5. The user receives a subscription URL like `https://<domain>:8443/sub/<token>`.
6. When the client opens that URL, the bot dynamically builds a Base64 subscription from all synced servers.

### Additional VPN servers

Every extra VPS runs only the `agent/xray` side. The main bot talks to those nodes over their `agent_url`.

This is what makes multi-server subscriptions possible:

- one Telegram bot
- one main database
- many VPN nodes
- per-server access control

## Key Features

- Telegram Stars payments
- single active product model: `30 days`
- renew instead of duplicate buy for active users
- subscription reminders before expiration
- automatic subscription disable on expiry
- multi-server subscriptions
- per-server `public/restricted` access mode
- whitelist grants for restricted servers
- admin panel in Telegram
- language switch in `/settings` (`ru/en`)
- HTTPS subscription endpoint via Caddy

## User Commands

- `/start`
- `/help`
- `/subscription`
- `/instruction`
- `/settings`

Admins also get:

- `/admin`

## Admin Panel

The admin panel is inline-button driven and currently supports:

- stats
- servers list
- server access mode toggle: `public` or `restricted`
- whitelist grant or revoke for a specific `tg_id`
- users lookup by `tg_id`
- issue subscription
- renew subscription
- revoke subscription
- ban and unban users
- plan price editing

## Repository Layout

```text
.
|-- agent/
|   |-- app/
|   |-- Dockerfile
|   |-- entrypoint.sh
|   |-- pyproject.toml
|   |-- template.json
|   `-- uv.lock
|-- bot/
|   |-- alembic/
|   |-- src/
|   |-- Dockerfile
|   |-- Dockerfile.deploy
|   |-- pyproject.toml
|   `-- uv.lock
|-- deploy/
|   `-- vps/
|       |-- add_server.py
|       |-- bot.env.example
|       |-- Caddyfile
|       |-- docker-compose.yml
|       |-- README.md
|       `-- vpn.env.example
`-- README.md
```

## Bot Responsibilities

The bot code in [bot/src](bot/src):

- stores users, plans, subscriptions, payments, servers, and whitelist grants
- exposes `/sub/{token}` over HTTP
- generates the final Base64 subscription content on the fly
- syncs UUIDs to all allowed servers through `agent`
- removes expired users from servers
- sends renewal reminders
- registers Telegram commands

Relevant files:

- [bot/src/main.py](bot/src/main.py)
- [bot/src/services/subscription_service.py](bot/src/services/subscription_service.py)
- [bot/src/services/server_access_service.py](bot/src/services/server_access_service.py)
- [bot/src/handlers/admin.py](bot/src/handlers/admin.py)
- [bot/src/handlers/user.py](bot/src/handlers/user.py)

## Agent Responsibilities

The `agent` code in [agent](agent):

- bootstraps `Reality` keys on first start
- creates `/data/agent.env`
- builds the live Xray config from [agent/template.json](agent/template.json)
- keeps existing clients across restarts
- exposes:
  - `GET /health`
  - `POST /client/add`
  - `POST /client/remove`
  - `POST /client/bulk`
  - `GET /sub/{uuid}`

Relevant files:

- [agent/entrypoint.sh](agent/entrypoint.sh)
- [agent/app/main.py](agent/app/main.py)
- [agent/app/config.py](agent/app/config.py)

## Subscription Generation

There is no static config file per user.

Instead:

1. the bot loads the subscription by `sub_token`
2. checks that it is active
3. reconciles allowed servers for the user
4. requests one VLESS URI from each synced server `agent`
5. normalizes the URI parameters
6. joins all server URIs into one text payload
7. returns Base64 to the client from `/sub/{token}`

This logic lives in [bot/src/services/subscription_service.py](bot/src/services/subscription_service.py).

## Access Control

Server access is stored in the database.

Each server has an `access_mode`:

- `public` - available to every user with an active subscription
- `restricted` - available only to users listed in `server_access_grants`

When access changes, the bot reconciles subscriptions and updates server-side UUIDs automatically.

Relevant models:

- [bot/src/models/server.py](bot/src/models/server.py)
- [bot/src/models/server_access.py](bot/src/models/server_access.py)
- [bot/src/models/subscription.py](bot/src/models/subscription.py)

## Plans

The project currently enforces a single active plan:

- `30 days`

That behavior is applied during bootstrap in [bot/src/core/bootstrap.py](bot/src/core/bootstrap.py).

The price can be changed from the admin panel via:

1. `/admin`
2. `Тарифы`
3. select the plan
4. send the new price

## Deployment Overview

The main production deploy lives in [deploy/vps](deploy/vps).

Important files:

- [deploy/vps/docker-compose.yml](deploy/vps/docker-compose.yml)
- [deploy/vps/Caddyfile](deploy/vps/Caddyfile)
- [deploy/vps/bot.env.example](deploy/vps/bot.env.example)
- [deploy/vps/vpn.env.example](deploy/vps/vpn.env.example)

Notes:

- the bot usually listens internally on `127.0.0.1:8080`
- Caddy exposes the HTTPS subscription endpoint, commonly on `:8443`
- Xray listens on the port from `vpn.env`, often `9443` if `443` is already occupied
- the root page `/` is intentionally hidden and returns `404`
- the only public bot endpoints that matter are `/health` and `/sub/{token}`

## Adding a New Server

There is now a helper script for almost-automatic server provisioning:

- [deploy/vps/add_server.py](deploy/vps/add_server.py)

It:

1. connects to the new VPS
2. uploads the `agent`
3. starts the remote VPN node
4. reads the generated `agent.env`
5. registers the new server in the main database

Example:

```bash
python deploy/vps/add_server.py \
  --main-host MAIN_SERVER_IP \
  --main-password YOUR_MAIN_ROOT_PASSWORD \
  --new-host NEW_SERVER_IP \
  --new-password YOUR_NEW_ROOT_PASSWORD \
  --server-name "🇩🇪 Germany" \
  --server-domain de.1-2-3-4.sslip.io
```

The flag emoji prefix in `--server-name` is parsed automatically.

## Local Notes

- SQLite is the default runtime database for the VPS deploy
- the bot can also use PostgreSQL through `DATABASE_URL`
- `PUBLIC_BASE_URL` is the source of truth for user-facing subscription links
- server bootstrap reads values from `/vpn-data/agent.env`

## Status of Old Docs

If you see references to:

- public landing pages on `/`
- nginx/certbot as the main public path
- multiple default plans
- direct user-facing VLESS links in `/subscription`

those are outdated and should not be treated as current architecture.
