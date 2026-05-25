# Aegis VPN — Bot

Telegram bot that sells and manages VPN subscriptions: it owns the database
(users, plans, subscriptions, payments, servers, access grants), serves the
subscription endpoint, and syncs client UUIDs to every node's [agent](../agent).

See the [top-level README](../README.md) for the full architecture and the
[deploy guide](../deploy/vps/README.md) for production.

## Layout

```
src/
├── core/        config, database, logger, bootstrap, migrations
├── models/      SQLAlchemy models
├── services/    business logic (admin, user, subscription, server access, i18n, telegraph, agent client)
├── handlers/    aiogram routers — UI only
│   ├── admin/   panel, servers, plans, users (per-domain sub-routers)
│   ├── user/    start, privacy, subscription, settings
│   └── payment.py
├── middlewares/ identity sync
├── privacy/     privacy policy (ru/en)
└── main.py      bot + HTTP app entry point
```

Handlers build the Telegram UI; all database access and state changes live in
`services/`. The schema is created from the models and kept current by the
idempotent migrations in `core/migrations.py` on startup (no Alembic).

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev          # install deps (incl. ruff + pytest)
cp ../.env.example .env       # then fill in BOT_TOKEN, ADMIN_IDS, ...
uv run python -m src.main     # run the bot

uv run ruff check .           # lint
uv run ruff format .          # format
uv run pytest -q              # tests
```

For a local Postgres + bot stack use `docker compose up --build`
(see `docker-compose.yml`). Production runs from `../deploy/vps`.

## Configuration

All settings come from environment variables / `.env` (see `src/core/config.py`).
Key ones: `BOT_TOKEN`, `ADMIN_IDS`, `TELEGRAM_MODE` (`webhook`/`polling`),
`DATABASE_URL` (defaults to SQLite at `SQLITE_PATH`), and
`PUBLIC_BASE_URL` (source of truth for subscription links).
