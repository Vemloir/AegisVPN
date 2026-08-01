"""Aegis VPN node agent: a thin HTTP control plane over a local Xray instance.

Routes only — the Xray interaction lives in :mod:`app.xray`, the connection
limiter in :mod:`app.connlimit`, and auth in :mod:`app.security`.
"""

import asyncio
import time
from contextlib import suppress
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from . import hysteria
from .certificate_sync import certificate_sync_loop
from .config import settings
from .connlimit import conn_limit_loop, set_override
from .control_loop import control_readiness, start_control_task
from .models import ClientAddRequest, ClientRemoveRequest, ConnLimitRequest, Hy2AuthRequest
from .reconcile import retry_pending_revocations
from .security import verify_token
from .xray import (
    build_client_record,
    build_subscription_query,
    config_lock,
    find_vless_inbound,
    get_online_count,
    get_online_emails,
    get_xray_config,
    list_vless_inbounds,
    query_traffic_stats,
    reload_xray,
    save_xray_config,
    xray_api_add,
    xray_api_remove,
)

app = FastAPI(title="Aegis VPN Agent")
_control_task: asyncio.Task | None = None
_certificate_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_background_tasks() -> None:
    global _certificate_task, _control_task
    # Always run the loop: even with the node default disabled (conn_limit=0),
    # per-user overrides pushed by the bot must still be enforced.
    asyncio.create_task(conn_limit_loop())
    print(f"conn-limit enforcement on: default {settings.conn_limit} IPs/user, every {settings.conn_limit_interval}s")
    # Populate the Hysteria2 user set from the on-disk xray config so auth works
    # immediately. No-op (empty set) on nodes without any vless clients.
    await hysteria.refresh()
    await retry_pending_revocations()
    _control_task = start_control_task()
    if settings.hy2_enabled and settings.control_mode != "off":
        _certificate_task = asyncio.create_task(
            certificate_sync_loop(),
            name="hy2-certificate-sync",
        )


@app.on_event("shutdown")
async def _stop_background_tasks() -> None:
    global _certificate_task, _control_task
    for task in (_control_task, _certificate_task):
        if task is None:
            continue
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    _control_task = None
    _certificate_task = None


@app.get("/health")
async def health():
    async with config_lock:
        config = await get_xray_config()
    unique_clients: set[str] = set()
    for inbound in list_vless_inbounds(config):
        for client in inbound.get("settings", {}).get("clients", []):
            client_id = client.get("id")
            if client_id:
                unique_clients.add(client_id)
    control: dict[str, object] | None = None
    if settings.control_mode != "off":
        control = control_readiness()
        task_alive = _control_task is not None and not _control_task.done()
        stale_after = max(120, int(settings.control_timeout_seconds) * 3)
        now = time.time()
        last_sync = control.get("last_sync_at")
        last_telemetry = control.get("last_telemetry_at")
        activity_fresh = all(
            isinstance(value, (int, float)) and now - value <= stale_after for value in (last_sync, last_telemetry)
        )
        if not task_alive or not control.get("supervisor_running") or not activity_fresh:
            raise HTTPException(
                status_code=503,
                detail={"status": "not-ready", "control": control},
            )
    return {
        "status": "ok",
        "clients": len(unique_clients),
        "control": control,
    }


@app.post("/conn-limit", dependencies=[Depends(verify_token)])
async def conn_limit(req: ConnLimitRequest):
    """Set or clear a per-user simultaneous-connection override.

    limit=None clears it (node default applies); 0 means unlimited; >0 caps to
    that many concurrent source IPs. Persisted across restarts.
    """
    set_override(req.user_id, req.limit)
    return {"status": "ok", "user_id": req.user_id, "limit": req.limit}


@app.get("/online", dependencies=[Depends(verify_token)])
async def online():
    return {"online": await get_online_count()}


@app.get("/online-emails", dependencies=[Depends(verify_token)])
async def online_emails():
    """Emails with at least one live session right now (authoritative online state).

    The bot maps these to per-device emails (user_X_sub_Y_dev_Z) to show, exactly,
    which device is connected and to which node — no traffic-delta guessing.
    """
    online_emails_set = set(await get_online_emails())
    online_emails_set.update(await hysteria.online())
    return {"emails": list(online_emails_set)}


@app.post("/hy2/auth")
async def hy2_auth(req: Hy2AuthRequest):
    """Hysteria2 connect-time auth callback (loopback only, no verify_token).

    Hy2 POSTs {addr, auth, tx} on every new connection; `auth` is the client's
    xray UUID. We answer {"ok": bool, "id": <email>} so Hy2 keys traffic on the
    same email as xray.
    """
    return hysteria.authenticate(req.auth)


@app.post("/client/add", dependencies=[Depends(verify_token)])
async def add_client(req: ClientAddRequest):
    async with config_lock:
        config = await get_xray_config()
        added = False
        api_ok = True
        for inbound in config.get("inbounds", []):
            if inbound.get("protocol") != "vless":
                continue
            clients = inbound.setdefault("settings", {}).setdefault("clients", [])
            if any(c.get("id") == req.uuid for c in clients):
                continue
            record = build_client_record(req.uuid, req.email, inbound)
            clients.append(record)  # persist for restarts
            added = True
            # apply live, no restart
            if not await xray_api_add(inbound, record):
                api_ok = False
        if added:
            await save_xray_config(config)
            hysteria.refresh_from_config(config)  # let Hy2 auth the new client
            if not api_ok:
                reload_xray()  # fallback only if the live API path failed
    return {"status": "ok", "added": added}


@app.post("/client/remove", dependencies=[Depends(verify_token)])
async def remove_client(req: ClientRemoveRequest):
    async with config_lock:
        config = await get_xray_config()
        removed = False
        api_ok = True
        removed_emails: set[str] = set()
        for inbound in config.get("inbounds", []):
            if inbound.get("protocol") != "vless":
                continue
            clients = inbound.setdefault("settings", {}).setdefault("clients", [])
            gone = [c for c in clients if c.get("id") == req.uuid]
            new_clients = [c for c in clients if c.get("id") != req.uuid]
            if len(new_clients) == len(clients):
                continue
            inbound["settings"]["clients"] = new_clients
            removed = True
            tag = inbound.get("tag")
            for c in gone:
                email = c.get("email")
                if email:
                    removed_emails.add(email)
                if tag and email and not await xray_api_remove(tag, email):
                    api_ok = False
        if removed:
            await save_xray_config(config)
            # Order matters: stop authenticating the user FIRST so a reconnect
            # fails, THEN drop their live QUIC session. Hy2 never re-auths, so
            # kicking without blocking would just let them reconnect.
            hysteria.refresh_from_config(config)
            await hysteria.kick(list(removed_emails))
            if not api_ok:
                reload_xray()
    return {"status": "ok", "removed": removed}


@app.post("/client/bulk", dependencies=[Depends(verify_token)])
async def bulk_add_clients(reqs: list[ClientAddRequest]):
    async with config_lock:
        config = await get_xray_config()
        changed = False
        api_ok = True
        for inbound in config.get("inbounds", []):
            if inbound.get("protocol") != "vless":
                continue
            clients = inbound.setdefault("settings", {}).setdefault("clients", [])
            existing_uuids = {c.get("id") for c in clients}
            for req in reqs:
                if req.uuid in existing_uuids:
                    continue
                record = build_client_record(req.uuid, req.email, inbound)
                clients.append(record)
                existing_uuids.add(req.uuid)
                changed = True
                if not await xray_api_add(inbound, record):
                    api_ok = False
        if changed:
            await save_xray_config(config)
            hysteria.refresh_from_config(config)  # let Hy2 auth the new clients
            if not api_ok:
                reload_xray()
    return {"status": "ok", "changed": changed}


@app.get("/sub/{token}", dependencies=[Depends(verify_token)])
async def get_subscription(token: str):
    uuid = token
    async with config_lock:
        config = await get_xray_config()

    exists = any(
        c.get("id") == uuid
        for inbound in list_vless_inbounds(config)
        for c in inbound.get("settings", {}).get("clients", [])
    )
    if not exists:
        raise HTTPException(status_code=404, detail="User not found")

    inbound = find_vless_inbound(config, preferred_network="xhttp")
    query = urlencode(build_subscription_query(inbound))
    label = quote(settings.host_ip, safe="")
    port = inbound.get("port") or settings.xray_port
    vless_uri = f"vless://{uuid}@{settings.host_ip}:{port}?{query}#{label}"

    # Return raw text. Bot will aggregate and encode to base64.
    return PlainTextResponse(vless_uri)


@app.get("/stats", dependencies=[Depends(verify_token)])
async def get_stats():
    stats = await query_traffic_stats()
    # Merge Hy2 traffic into the same email keys (sum uplink/downlink). No-op
    # when Hy2 is disabled (hysteria.traffic() returns {}).
    for email, counters in (await hysteria.traffic()).items():
        bucket = stats.setdefault(email, {"uplink": 0, "downlink": 0})
        bucket["uplink"] += counters.get("uplink", 0)
        bucket["downlink"] += counters.get("downlink", 0)
    return {"stats": stats}


@app.get("/sub-fast/{token}", dependencies=[Depends(verify_token)])
async def get_fast_subscription(token: str):
    uuid = token
    async with config_lock:
        config = await get_xray_config()

    exists = any(
        c.get("id") == uuid
        for inbound in list_vless_inbounds(config)
        for c in inbound.get("settings", {}).get("clients", [])
    )
    if not exists:
        raise HTTPException(status_code=404, detail="User not found")

    inbound = find_vless_inbound(config, preferred_network="tcp")
    query = urlencode(build_subscription_query(inbound))
    target_host = settings.fast_host_ip or settings.host_ip
    label = quote(target_host, safe="")
    port = inbound.get("port") or settings.xray_tcp_port or settings.xray_port
    vless_uri = f"vless://{uuid}@{target_host}:{port}?{query}#{label}"

    # Return raw text. Bot will aggregate and encode to base64.
    return PlainTextResponse(vless_uri)
