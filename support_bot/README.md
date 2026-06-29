# AegisVPN Support Bot

A minimal two-way relay between users and operators, used as the official
feedback channel (a direct contact, not a group — as required by the payment
provider). Runs as a separate Telegram bot (`@AegisVPNsupportBot`).

## How it works

- A user opens the bot, taps **Start**, and writes a message.
- The bot copies that message to every operator in `ADMIN_IDS`, preceded by a
  plain banner with the user's name / id / @username.
- An operator answers by **replying** (reply) to the forwarded message. The
  reply (text or media) is copied back to the user.
- The `forwarded-message → user` mapping is kept in sqlite (`DB_PATH`), so a
  ticket can still be answered after a bot restart.

No inbound port: the bot uses long-polling.

## Configuration

Copy `.env.example` to `.env` and fill it in (see field comments). The token is
read from the environment and is never committed.

## Develop / test

```bash
uv run pytest        # routing + storage unit tests (no network, no Telegram)
```

The relay decision logic lives in `src/routing.py` (pure, fully tested); the
persistence in `src/storage.py`; aiogram wiring in `src/main.py`.

## Deploy

Built as the `support-bot` service in `deploy/vps/docker-compose.yml`. Provide
`deploy/vps/support.env` (from `support.env.example`) on the host. Bring it up
with the rest of the stack:

```bash
docker compose up -d --build support-bot
```
