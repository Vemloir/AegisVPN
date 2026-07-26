# Aegis VPN — Node Agent

A node-local reconciler that runs on every VPN server alongside
[Xray](https://github.com/XTLS/Xray-core) (VLESS + Reality + xtls-rprx-vision).
It initiates outbound mTLS HTTPS/TCP 443 connections to the central control
host, verifies immutable desired-state snapshots, and applies exact Xray,
Hysteria2, and connection-limit state. On first start it also generates Reality
keys and a legacy Agent token into `/data/agent.env`.

See the [top-level README](../README.md) for the full architecture.

## Layout

```
app/
├── config.py          settings (from /data/agent.env)
├── control_client.py  bounded mTLS sync/page/ack/telemetry client
├── control_loop.py    long poll, retry/backoff, cached expiry enforcement
├── control_models.py  versioned desired/applied state
├── reconcile.py       exact Xray/Hysteria/limit reconciliation
├── xray.py            atomic config I/O and live Xray API operations
├── connlimit.py       per-subscription simultaneous-IP limit
└── main.py            local/legacy FastAPI routes
entrypoint.sh     bootstraps keys + renders the live Xray config, then runs both
template.json     base Xray config (no-logs, stats/api/policy)
```

## Local and rollout API

These endpoints are retained for local Hysteria auth, diagnostics, and the
temporary `observe` rollout. After pull promotion Uvicorn binds to
`127.0.0.1`; TCP/8444 is also dropped at the firewall.

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
config. If a live operation fails, the fallback reload must return a healthy
Xray API before the generation can be acknowledged.

## Outbound control modes

- `off`: legacy behavior only.
- `observe`: download and verify snapshots without mutation or acknowledgement;
  push remains active from the fixed central IP.
- `apply`: reconcile exact state and acknowledge only after Xray, Hysteria,
  connection limits, and durable local state all succeed.

Snapshot pages bound each request but do not cap the number of devices. The
last verified snapshot is stored under `/data/control/`, so expiry is enforced
even while every central endpoint is unreachable.

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
