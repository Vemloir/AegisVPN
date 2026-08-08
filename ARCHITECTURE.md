# Architecture

## Overview

Aegis VPN is split into three main layers:

- `bot` - Telegram bot, subscription logic, admin panel, HTTP subscription endpoint
- `agent` - local control plane on each VPN server
- `xray` - actual VPN traffic engine

There is one main control server and one or more VPN nodes.

## Main Components

### 1. Bot

Located in [bot/src](C:/Users/detko/Documents/VPN/bot/src).

Responsibilities:

- handles Telegram commands and payments
- stores users, subscriptions, plans, servers, payments, and access grants
- exposes `/sub/{token}`
- builds subscription payloads dynamically
- syncs user UUIDs to VPN nodes
- sends expiration reminders
- removes expired subscriptions from servers

Important files:

- [bot/src/main.py](C:/Users/detko/Documents/VPN/bot/src/main.py)
- [bot/src/handlers/user.py](C:/Users/detko/Documents/VPN/bot/src/handlers/user.py)
- [bot/src/handlers/admin.py](C:/Users/detko/Documents/VPN/bot/src/handlers/admin.py)
- [bot/src/services/subscription_service.py](C:/Users/detko/Documents/VPN/bot/src/services/subscription_service.py)
- [bot/src/services/server_access_service.py](C:/Users/detko/Documents/VPN/bot/src/services/server_access_service.py)

### 2. Agent

Located in [agent](C:/Users/detko/Documents/VPN/agent).

Responsibilities:

- bootstraps Reality keys on first start
- creates `agent.env`
- creates and maintains the live Xray config
- adds and removes clients in local Xray
- exposes health and client-management API
- returns one VLESS URI for a given user UUID

Important files:

- [agent/entrypoint.sh](C:/Users/detko/Documents/VPN/agent/entrypoint.sh)
- [agent/app/main.py](C:/Users/detko/Documents/VPN/agent/app/main.py)
- [agent/template.json](C:/Users/detko/Documents/VPN/agent/template.json)

### 3. Xray

Managed by the `agent`.

Responsibilities:

- terminates VLESS + Reality connections
- authenticates clients by UUID
- carries real VPN traffic

## Deployment Layout

### Main VPS

Runs:

- `aegis-bot`
- `aegis-siteapi`
- `aegis-support-bot`
- `aegis-caddy`

Caddy is the only TCP/443 listener. The optional local `agent` and `xray`
services are gated behind the `local-exit` profile and are disabled on the
production control host.

Defined in [deploy/vps/docker-compose.yml](C:/Users/detko/Documents/VPN/deploy/vps/docker-compose.yml).

### Additional VPN VPS nodes

Usually run only:

- `agent`
- `xray`

They are registered in the main database and then become part of user subscriptions.

## Data Model

Core entities:

- `User`
- `Plan`
- `Subscription`
- `SubscriptionServer`
- `Server`
- `ServerAccessGrant`
- `Payment`

Main relationships:

- one `User` can have subscriptions
- one `Subscription` can be synced to many servers
- one `Server` can be public or restricted
- restricted servers use `ServerAccessGrant` as a allowlist

Relevant models:

- [bot/src/models/user.py](C:/Users/detko/Documents/VPN/bot/src/models/user.py)
- [bot/src/models/plan.py](C:/Users/detko/Documents/VPN/bot/src/models/plan.py)
- [bot/src/models/subscription.py](C:/Users/detko/Documents/VPN/bot/src/models/subscription.py)
- [bot/src/models/server.py](C:/Users/detko/Documents/VPN/bot/src/models/server.py)
- [bot/src/models/server_access.py](C:/Users/detko/Documents/VPN/bot/src/models/server_access.py)

## Request Flow

### A. User buys or renews VPN

1. User chooses a plan in Telegram.
2. Bot records the payment result.
3. Bot creates or extends `Subscription`.
4. Bot ensures the subscription has:
   - `sub_token`
   - `client_uuid`
5. Bot finds all servers available to this user.
6. Bot calls each server `agent` and adds the `client_uuid` to Xray.

### B. User opens subscription URL

1. Client requests `/sub/<token>`.
2. Bot finds the matching active subscription.
3. Bot checks whether it is expired.
4. Bot reconciles allowed servers for the user.
5. Bot requests one VLESS URI from each synced server `agent`.
6. Bot normalizes each URI.
7. Bot joins all URIs into one payload.
8. Bot Base64-encodes the result and returns it to the client.

### C. Subscription expires

1. Scheduler finds expired subscriptions.
2. Bot marks them inactive.
3. Bot removes `client_uuid` from all synced servers.
4. Subscription URL stops working and returns `404`.

## Access Control

Each server has one of two modes:

- `public`
- `restricted`

Behavior:

- `public`: all users with active subscriptions may receive this server
- `restricted`: only users in `server_access_grants` may receive this server

When access changes, the bot reconciles subscriptions and updates synced UUIDs on VPN nodes.

## Public HTTP Surface

Bot HTTP:

- `GET /health`
- `GET /sub/{token}`

Agent HTTP:

- `GET /health`
- `POST /client/add`
- `POST /client/remove`
- `POST /client/bulk`
- `GET /sub/{uuid}`

The root page `/` is intentionally hidden in the bot and returns `404`.

## HTTPS Layer

`Caddy` sits in front of the bot and proxies public HTTPS traffic to the internal bot HTTP server.

Typical setup:

- public: `https://<domain>/sub/<token>`
- legacy public alias: `https://<domain>:8443/sub/<token>`
- internal bot: `127.0.0.1:8080`

Relevant file:

- [deploy/vps/Caddyfile](C:/Users/detko/Documents/VPN/deploy/vps/Caddyfile)

## Configuration Sources

### Bot config

Read from environment via [bot/src/core/config.py](C:/Users/detko/Documents/VPN/bot/src/core/config.py).

Important values:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `PUBLIC_BASE_URL`
- `TELEGRAM_MODE`
- `DATABASE_URL` or SQLite path
- bootstrap server settings

### Agent config

Read from `/data/agent.env` and environment variables.

Important values:

- `AGENT_TOKEN`
- `XRAY_PORT`
- `PUBLIC_KEY`
- `SHORT_ID`
- `REALITY_DEST`
- `REALITY_SERVER_NAME`
- `HOST_IP`

## Bootstrap Logic

At startup, the bot bootstrap layer:

- initializes the DB if enabled
- ensures `users.language` exists
- enforces a single active `30-day` plan
- bootstraps the main local VPN server from `/vpn-data/agent.env`

Relevant file:

- [bot/src/core/bootstrap.py](C:/Users/detko/Documents/VPN/bot/src/core/bootstrap.py)

## Adding a New Server

The helper script [deploy/vps/add_server.py](C:/Users/detko/Documents/VPN/deploy/vps/add_server.py) automates most of the process.

It:

1. connects to the new VPS
2. uploads the `agent`
3. starts the remote VPN node
4. reads generated server credentials
5. registers the node in the main database

After that, the server becomes available for:

- public distribution
- restricted allowlist distribution
- inclusion in subscription payloads

## Summary

The system is built around one rule:

- the bot is the source of truth for users, subscriptions, and access
- agents are the source of truth for local server state
- Xray is the data plane only

That separation is what makes multi-server support, allowlist-based access, and near-automatic server expansion possible.
