# Aegis VPN

**Telegram bot:** [@AegisEcoVPN_bot](https://t.me/AegisEcoVPN_bot)
**Website:** [aegisvpn.org](https://aegisvpn.org)
**Support bot:** [@AegisVPNsupportBot](https://t.me/AegisVPNsupportBot)

Aegis VPN sells and manages VPN subscriptions backed by Xray (`VLESS + Reality`)
and, per location, an optional Hysteria2 (`hy2`) data plane, across one or more
VPS nodes. Subscriptions can be bought through the Telegram bot or through the
public website; both converge on the same database and the same payment
handling.

The project is a split architecture:

- `bot` stores users, subscriptions, plans, server access rules, referrals,
  devices, and serves the subscription URL and the website's public API.
- `agent` runs on each VPN server, manages the local Xray and (optionally)
  Hysteria2 state, and initiates an authenticated outbound control connection
  over HTTPS/TCP 443.
- `web` is the public marketing/checkout site (React SPA), built and served
  as static files.
- `support_bot` is a separate Telegram bot used as the official feedback
  channel between users and operators.
- `deploy/vps` contains the production compose stack(s) for the control host
  and VPN nodes.
- `Caddy` terminates HTTPS for the site, the bot subscription endpoint, and
  node control, and serves the built `web` SPA.

## Current Architecture

### Main VPS (control host)

The control host runs the database/bot plus the public site and the HTTPS
control endpoint, defined in
[deploy/vps/docker-compose.yml](deploy/vps/docker-compose.yml):

- `aegis-bot` - Telegram bot, SQLite DB, subscription endpoint
- `aegis-siteapi` - public website API (checkout, Telegram login, plans,
  locations, legal docs) - same code/DB as the bot, so the site can never
  disagree with what the bot sells
- `aegis-support-bot` - the separate support Telegram bot (long-polling,
  reads the main bot's SQLite read-only for locale)
- `aegis-caddy` - HTTPS reverse proxy for the site, bot, and node control;
  also serves the built `web` SPA
- `aegis-mtg` - optional MTProto proxy (opt-in via the `mtproxy` profile)

The `xray`, `agent`, and `hysteria` services are behind opt-in Compose
profiles (`local-exit`, `hysteria`). A normal control-host deployment must not
start `xray`/`agent`: Caddy owns TCP 443 exclusively. VPN exits normally run
on separate hosts via `docker-compose.node.yml`.

An optional HA variant of the control host (PostgreSQL + Patroni, HAProxy,
etcd, Cloudflare DNS failover) lives in [deploy/vps/ha](deploy/vps/ha) for
operators who need multi-instance control-host failover; the default deploy
described above uses SQLite and a single control host.

Typical flow:

1. A user buys or renews a subscription in Telegram or on the website
   (Telegram Stars, or SBP bank transfer via Platega).
2. The bot creates or updates a subscription record in SQLite.
3. The bot publishes an immutable, paginated desired-state snapshot for every
   affected node.
4. Each node long-polls the mTLS control endpoint over outbound HTTPS/443,
   verifies the complete snapshot digest, and reconciles Xray (and Hysteria2,
   if enabled) live.
5. The user receives a subscription URL like `https://<domain>/sub/<token>`.
6. When the client opens that URL, the bot dynamically builds the
   subscription (a Base64 link list, or a full xray-JSON config for clients
   that support it) from all synced servers, honoring each user's per-location
   protocol choice.

### Additional VPN servers

Every extra VPS runs the `agent`/`xray` side and, optionally, Hysteria2 (the
`hysteria` Compose profile). Normal management is node-initiated; no public
Agent API is needed after a node is promoted to `pull`. The legacy push path
remains available only during the per-node `observe` canary and is restricted
to the fixed control-server IP.

This is what makes multi-server subscriptions possible:

- one Telegram bot (+ one public website)
- one main database
- many VPN nodes, each with its own protocol capabilities
- per-server access control

## Protocols

- **VLESS + Reality** (Xray) is the default protocol on every node. The
  default transport is TCP with XTLS Vision flow; `xhttp` is available as an
  alternate transport per location.
- **Hysteria2** (`hy2`) is an opt-in, per-location alternative data plane. A
  server is "Hy2-capable" only when it has Hysteria2 enabled, a port, and a
  certificate SNI configured (`Server.hy2_capable`); it exposes its own
  camouflage SNI and obfuscation password, separate from the certificate SNI.
  Users pick VLESS or Hysteria2 per location from `/settings` → locations; the
  choice is stored per user/server and only takes effect where the server
  actually supports it.
- The subscription builder emits a `hysteria2://` link for locations pinned to
  Hy2. For xray-JSON clients (Happ, v2rayTun, v2rayNG, v2rayN, NekoBox/NekoRay,
  Streisand, FoXray, Varmlen, INCY - matched by User-Agent), both VLESS and Hy2
  locations are converted into standalone xray-JSON config objects - the Hy2
  one via a `hysteria` outbound, which is exactly the config those clients
  build from a `hysteria2://` link themselves; plain xray-core has no
  Hysteria2 outbound, so that JSON form only works because those clients
  bundle the Xray fork that does. If any single entry can't be expressed as a
  config object, the whole response falls back to the Base64 link list as a
  safety net, so nothing is silently dropped.
- UDP-filtering providers can be worked around per node with a Hysteria2
  UDP/2053 fallback listener, without touching the default UDP/443 listener
  (see [deploy/vps/README.md](deploy/vps/README.md)).
- An optional per-node Cloudflare WARP egress can route AI-service traffic
  (Gemini, OpenAI, etc., by geosite category) and Microsoft domains through a
  clean consumer IP without moving the rest of the node's traffic off its own
  IP; game traffic is explicitly kept off WARP.
- Nodes can optionally act as cascade entry/exit hops (`node_role`,
  `cascade-v2` capability) to relay a location's traffic through another node;
  this is dormant by default and does not change the default single-hop
  behavior of existing locations.

## Key Features

- Telegram Stars and SBP (bank transfer via Platega) payments, in the bot and
  on the website
- multiple concurrent plans, admin-managed (create/edit price/delete, and one
  plan flagged as the site's reference "base" plan); a single `30-day` plan is
  only the bootstrap fallback when no plan has been configured yet
- lifetime subscriptions (admin-issuable, `days = 0`)
- renew instead of duplicate buy for active users
- referral program: bonus days for the referrer when a referred user is
  active
- subscription reminders before expiration
- automatic subscription disable on expiry
- multi-server subscriptions with per-location protocol choice (VLESS/Reality
  or Hysteria2) and per-location VLESS transport choice
- outbound mTLS node control with exact offline recovery
- unlimited registered devices via paginated snapshots (no device-count cap),
  with an optional per-user simultaneous-connection (session) limit enforced
  by the agent - the two are tracked and configured separately
- device management in `/settings`: list, suspend/resume, remove, approximate
  add-location via GeoIP
- subscription link reissue (rotate `sub_token`) and self-service account
  deletion, both from `/settings`
- per-server `public/restricted` access mode with allowlist grants
- admin panel in Telegram
- language switch in `/settings` (`ru/en`)
- HTTPS subscription endpoint via Caddy
- public website (aegisvpn.org) with its own checkout and Telegram Login,
  sharing the bot's database
- separate support bot for two-way user/operator feedback
- optional MTProto proxy per node

## User Commands

- `/start`
- `/subscription`
- `/settings`
- `/info` (about, legal documents, news)

`/help` still works when typed but is no longer listed in the bot's command
menu.

Admins also get:

- `/admin`

## Admin Panel

The admin panel is inline-button driven and currently supports:

- stats
- servers list, with active/inactive toggle and access mode toggle
  (`public`/`restricted`)
- allowlist grant or revoke for a specific `tg_id` on a restricted server
- users lookup by `tg_id`
- issue subscription (fixed-length or lifetime), renew, revoke
- per-user simultaneous-connection limit
- ban and unban users
- bulk-extend all active subscriptions
- plans: create, edit price, delete, set the reference "base" plan
- download a database backup

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
|   |   |-- api/          # public website API (checkout, auth, legal)
|   |   |-- control/      # node control-plane server (mTLS)
|   |   |-- handlers/     # Telegram user + admin handlers
|   |   |-- models/
|   |   |-- scheduler/
|   |   `-- services/
|   |-- Dockerfile
|   |-- Dockerfile.deploy
|   |-- pyproject.toml
|   `-- uv.lock
|-- support_bot/          # separate Telegram support bot
|-- web/                  # public website SPA (source; built to web/dist)
|-- deploy/
|   `-- vps/
|       |-- add_server.py
|       |-- bot.env.example
|       |-- Caddyfile
|       |-- control-plane/
|       |-- docker-compose.yml
|       |-- docker-compose.node.yml
|       |-- ha/            # optional HA control-host stack
|       |-- hysteria/
|       |-- README.md
|       `-- vpn.env.example
|-- docs/
`-- README.md
```

## Bot Responsibilities

The bot code in [bot/src](bot/src):

- stores users, plans, subscriptions, payments, servers, allowlist grants,
  referrals, and devices
- exposes `/sub/{token}`, `/sub-safe/{token}`, `/sub-fast/{token}` over HTTP
- generates the final subscription content on the fly (Base64 link list, or
  xray-JSON for recognized clients)
- syncs UUIDs to all allowed servers through `agent`
- removes expired users from servers
- sends renewal reminders
- registers Telegram commands
- runs the public website API (`src/api`) alongside the bot, against the same
  database

Relevant files:

- [bot/src/main.py](bot/src/main.py)
- [bot/src/api/main.py](bot/src/api/main.py)
- [bot/src/services/subscription_service.py](bot/src/services/subscription_service.py)
- [bot/src/services/server_access_service.py](bot/src/services/server_access_service.py)
- [bot/src/handlers/admin](bot/src/handlers/admin)
- [bot/src/handlers/user](bot/src/handlers/user)

## Agent Responsibilities

The `agent` code in [agent](agent):

- bootstraps `Reality` keys on first start
- creates `/data/agent.env`
- builds the live Xray config from [agent/template.json](agent/template.json)
- manages the local Hysteria2 process when enabled (auth callback, kick,
  stats) and merges its stats into the same per-user view as Xray
- keeps existing clients across restarts
- exposes:
  - `GET /health`
  - `POST /client/add`
  - `POST /client/remove`
  - `POST /client/bulk`
  - `POST /conn-limit`
  - `GET /online`, `GET /online-emails`
  - `POST /hy2/auth` (Hysteria2 auth callback, unauthenticated on loopback)
  - `GET /stats`
  - `GET /sub/{uuid}`, `GET /sub-fast/{uuid}`
- long-polls `/api/node/v1/*` on the central control host using a per-node mTLS
  certificate and token
- reconciles complete desired state, including revocations made while offline

Relevant files:

- [agent/entrypoint.sh](agent/entrypoint.sh)
- [agent/app/main.py](agent/app/main.py)
- [agent/app/config.py](agent/app/config.py)
- [agent/app/hysteria.py](agent/app/hysteria.py)
- [agent/app/connlimit.py](agent/app/connlimit.py)

## Subscription Generation

There is no static config file per user.

Instead:

1. the bot loads the subscription by `sub_token`
2. checks that it is active
3. reconciles allowed servers for the user
4. for each server, resolves the user's chosen protocol/transport for that
   location (VLESS/TCP by default, VLESS/xhttp, or Hysteria2 where capable)
5. builds a `vless://` or `hysteria2://` URI accordingly, from the node's
   authenticated outbound telemetry
6. normalizes the URI parameters
7. joins all server URIs into one text payload (or, for recognized xray-JSON
   clients, builds a full config array instead)
8. returns the result, Base64-encoded for the link-list form, from
   `/sub/{token}` (and the `-safe`/`-fast` profile variants)

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

Plans are admin-managed and multiple can be active at once. Each plan has a
`days` duration (`0` means a lifetime plan), a Telegram Stars price, and an
optional RUB price for the SBP/card path (no RUB price means that payment
method isn't offered for that plan). One plan can be flagged `is_base`: the
site shows it first and prices every other plan's per-month rate against it.

On startup the bot first seeds/updates plans from `BOOTSTRAP_PLANS_JSON` (if
set), then, only if no plan is active afterward, creates a single `30-day`
plan as a last-resort fallback default. That bootstrap step is in
[bot/src/core/bootstrap.py](bot/src/core/bootstrap.py); day-to-day plan
management happens from the admin panel:

1. `/admin`
2. `Тарифы`
3. create, edit price, delete, or set the base plan

## Deployment Overview

The main production deploy lives in [deploy/vps](deploy/vps).

Important files:

- [deploy/vps/docker-compose.yml](deploy/vps/docker-compose.yml) (control host)
- [deploy/vps/docker-compose.node.yml](deploy/vps/docker-compose.node.yml) (VPN node)
- [deploy/vps/Caddyfile](deploy/vps/Caddyfile)
- [deploy/vps/bot.env.example](deploy/vps/bot.env.example)
- [deploy/vps/vpn.env.example](deploy/vps/vpn.env.example)

Notes:

- the bot usually listens internally on `127.0.0.1:8080`, the site API on
  `127.0.0.1:8000`
- Caddy exposes the public site, the subscription endpoints, and the
  dedicated mTLS control hostname; it also serves the built `web` SPA on the
  site's own domain
- node control always uses ordinary HTTPS on TCP/443, not WireGuard
- Xray listens on the port from `vpn.env`, often `9443` if `443` is already
  occupied; Hysteria2 (when enabled) listens on UDP, typically `443` with an
  optional per-node UDP/2053 fallback
- inside the bot's own HTTP app, the root path `/` and `/info.json` return
  `404` - the bot itself never serves a landing page; the public marketing
  site is a separate static SPA (`web/`) that Caddy serves on the site's own
  virtual host, proxying `/api/*` to `siteapi` and `/sub*` to the bot
- the bot's own public endpoints are `/health` and `/sub*`; the site adds
  `/api/*` (checkout, login, plans, locations, legal docs) via `siteapi`

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
  --main-host-fingerprint SHA256:MAIN_HOST_KEY \
  --new-host NEW_SERVER_IP \
  --new-host-fingerprint SHA256:NEW_HOST_KEY \
  --server-name "🇩🇪 Germany" \
  --server-domain NEW_SERVER_IP \
  --reality-dest example.org:443 \
  --reality-server-name example.org \
  --control-url https://control.example.com \
  --control-ca-dir /secure/operator/aegis-control-ca
```

SSH passwords are requested through a hidden `getpass` prompt and never appear
in argv or shell history. Prefer `--main-key-file` and `--new-key-file`. Host-key
fingerprints must come from the VPS provider or another authenticated channel;
an existing verified `~/.ssh/known_hosts` entry can be used instead. Cloud-image
users such as `ubuntu` are supported through `sudo` without enabling root login
or password authentication.

The flag emoji prefix in `--server-name` is parsed automatically. Hysteria2 and
MTProto-proxy capability on a new node are configured separately (see
[deploy/vps/README.md](deploy/vps/README.md)).

CA initialization, one-node promotion, rollback, rotation, and incident
procedures are documented in
[docs/control-plane-operations.md](docs/control-plane-operations.md).

## Local Notes

- SQLite is the default runtime database for the VPS deploy
- the bot can also use PostgreSQL through `DATABASE_URL` (required for the
  optional HA control-host stack in `deploy/vps/ha`)
- `PUBLIC_BASE_URL` is the source of truth for user-facing subscription links
- `SITE_PUBLIC_URL` / `SUPPORT_PUBLIC_URL` point the subscription metadata and
  legal pages at the public website and support bot
- server bootstrap reads values from `/vpn-data/agent.env`

## License

This project is open source under the [MIT License](LICENSE).
