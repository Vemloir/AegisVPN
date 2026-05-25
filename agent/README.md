# Aegis VPN — Node Agent

A thin HTTP control plane that runs on every VPN node alongside
[Xray](https://github.com/XTLS/Xray-core) (VLESS + Reality + xtls-rprx-vision).
The [bot](../bot) calls it to add/remove clients, pull traffic stats, and build
per-node subscription URIs. On first start it generates the Reality keys and an
agent token into `/data/agent.env`.

See the [top-level README](../README.md) for the full architecture.

## Layout

```
app/
├── config.py     settings (from /data/agent.env)
├── models.py     request models
├── security.py   bearer-token auth
├── xray.py       Xray control plane: config I/O, live add/remove, sub URIs, stats
├── connlimit.py  per-subscription simultaneous-IP limit
└── main.py       FastAPI app — routes only
entrypoint.sh     bootstraps keys + renders the live Xray config, then runs both
template.json     base Xray config (no-logs, stats/api/policy)
```

## API

All endpoints except `/health` require `Authorization: Bearer <AGENT_TOKEN>`.

| Method | Path             | Purpose                              |
|--------|------------------|--------------------------------------|
| GET    | `/health`        | status + unique client count         |
| POST   | `/client/add`    | add one client (live, no restart)    |
| POST   | `/client/remove` | remove one client                    |
| POST   | `/client/bulk`   | add many clients                     |
| GET    | `/sub/{uuid}`    | VLESS URI (xhttp transport)          |
| GET    | `/sub-fast/{uuid}`| VLESS URI (tcp + Vision transport)  |
| GET    | `/stats`         | per-client traffic counters          |

Client changes are applied live via `xray api adu`/`rmu` and persisted to the
config (a `pkill -HUP` restart is only a fallback if the live API path fails).

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
uv run ruff check .
uv run pytest -q
```

Deployment is automated by [`../deploy/vps/add_server.py`](../deploy/vps/add_server.py),
which uploads this agent to a new VPS, starts it, and registers the node in the
bot's database.
