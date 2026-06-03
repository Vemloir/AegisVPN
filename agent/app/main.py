"""Aegis VPN node agent: a thin HTTP control plane over a local Xray instance.

Routes only — the Xray interaction lives in :mod:`app.xray`, the connection
limiter in :mod:`app.connlimit`, and auth in :mod:`app.security`.
"""

import asyncio
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from .config import settings
from .connlimit import conn_limit_loop
from .models import ClientAddRequest, ClientRemoveRequest
from .security import verify_token
from .xray import (
    build_client_record,
    build_subscription_query,
    config_lock,
    find_vless_inbound,
    get_online_count,
    get_xray_config,
    list_vless_inbounds,
    query_traffic_stats,
    reload_xray,
    save_xray_config,
    xray_api_add,
    xray_api_remove,
)

app = FastAPI(title="Aegis VPN Agent")


@app.on_event("startup")
async def _start_background_tasks() -> None:
    if settings.conn_limit > 0:
        asyncio.create_task(conn_limit_loop())
        print(f"conn-limit enforcement on: max {settings.conn_limit} IPs/sub, every {settings.conn_limit_interval}s")


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
    return {"status": "ok", "clients": len(unique_clients)}


@app.get("/online", dependencies=[Depends(verify_token)])
async def online():
    return {"online": await get_online_count()}


@app.get("/hy2-online", dependencies=[Depends(verify_token)])
async def hy2_online():
    """Active Hysteria2 connections on this node (0 if no local Hysteria2)."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            async with sess.get("http://127.0.0.1:8088/traffic", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {"online": len(data)}
    except Exception:
        pass
    return {"online": 0}


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
            if not api_ok:
                reload_xray()  # fallback only if the live API path failed
    return {"status": "ok", "added": added}


@app.post("/client/remove", dependencies=[Depends(verify_token)])
async def remove_client(req: ClientRemoveRequest):
    async with config_lock:
        config = await get_xray_config()
        removed = False
        api_ok = True
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
                if tag and email and not await xray_api_remove(tag, email):
                    api_ok = False
        if removed:
            await save_xray_config(config)
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
    return {"stats": await query_traffic_stats()}


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
