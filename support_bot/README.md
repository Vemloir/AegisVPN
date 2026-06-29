# AegisVPN Support Bot

A ticket-based support bot (`@AegisVPNsupportBot`) — the official feedback
channel (a direct contact, not a group, as required by the payment provider).
Standalone Telegram bot, long-polling, no inbound port.

## User flow

- **Создать тикет** — FSM: enter a title, then the message. A ticket is opened.
- **Мои тикеты** — paginated list (5 per page, « / » navigation). Tap a ticket
  to see the full thread and, while it is open, **Написать сообщение** or
  **Закрыть тикет**. A closed ticket is read-only (open a new one for a new
  question).

## Operator flow

- On every update (new ticket / new user message) each operator in `ADMIN_IDS`
  gets **one** consolidated message: who wrote, status, the full conversation,
  and an inline **Закрыть тикет** button.
- Reply by **replying** (reply) to that message — the text is delivered to the
  user inside their ticket.
- Close anytime via the inline button; the user is notified.

## Storage

sqlite at `DB_PATH`: `tickets`, `ticket_messages`, and `admin_msg_map`
(operator-message → ticket, so reply-to resolves correctly). Survives restarts.

## Configuration

Copy `.env.example` to `.env` (or provide `deploy/vps/support.env`) and fill in
`SUPPORT_BOT_TOKEN` + `ADMIN_IDS`. The token is read from the environment and is
never committed.

## Develop / test

```bash
uv run pytest      # storage + pagination + render unit tests (no network)
```

Logic is split into pure, tested modules — `storage.py` (DB), `pagination.py`
(list math), `render.py` (thread/history text) — with thin aiogram handlers in
`handlers.py`.

## Deploy

Built as the `support-bot` service in `deploy/vps/docker-compose.yml`:

```bash
docker compose up -d --build support-bot
```
