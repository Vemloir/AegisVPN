import json
import os
import subprocess
import tempfile
import asyncio
import aiofiles
from typing import List
from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import PlainTextResponse
from urllib.parse import urlencode, quote

from .config import settings
from .models import ClientAddRequest, ClientRemoveRequest

security = HTTPBearer()
config_lock = asyncio.Lock()


def list_vless_inbounds(config: dict) -> list[dict]:
    return [inbound for inbound in config.get("inbounds", []) if inbound.get("protocol") == "vless"]


def find_vless_inbound(config: dict, preferred_network: str | None = None) -> dict:
    inbounds = list_vless_inbounds(config)
    if preferred_network:
        preferred_network = preferred_network.lower()
        for inbound in inbounds:
            if get_transport_type(inbound) == preferred_network:
                return inbound
    return inbounds[0] if inbounds else {}

def get_transport_type(inbound: dict) -> str:
    stream = inbound.get("streamSettings", {})
    network = (stream.get("network") or "tcp").lower()
    return "xhttp" if network == "xhttp" else "tcp"

def build_client_record(uuid: str, email: str, inbound: dict) -> dict:
    record = {
        "id": uuid,
        "email": email,
    }
    if get_transport_type(inbound) != "xhttp":
        record["flow"] = "xtls-rprx-vision"
    return record

def build_subscription_query(inbound: dict) -> list[tuple[str, str]]:
    stream = inbound.get("streamSettings", {})
    reality = stream.get("realitySettings", {})
    transport = get_transport_type(inbound)
    public_key = settings.public_key
    short_id = settings.short_id
    fingerprint = settings.fingerprint
    if transport != "xhttp":
        public_key = settings.public_key_tcp or public_key
        short_id = settings.short_id_tcp or short_id
        fingerprint = settings.tcp_fingerprint or fingerprint
    query: list[tuple[str, str]] = [
        ("type", transport),
        ("security", "reality"),
        ("encryption", "none"),
        ("sni", (reality.get("serverNames") or [settings.reality_server_name])[0]),
        ("fp", fingerprint),
        ("pbk", public_key),
        ("sid", short_id),
        ("spx", "/"),
    ]
    if transport == "xhttp":
        xhttp_settings = stream.get("xhttpSettings", {})
        query.append(("path", xhttp_settings.get("path") or settings.xhttp_path))
        query.append(("mode", xhttp_settings.get("mode") or settings.xhttp_mode))
        if settings.packet_encoding:
            query.append(("packetEncoding", settings.packet_encoding))
    else:
        query.append(("headerType", "none"))
        query.append(("flow", "xtls-rprx-vision"))
    if settings.packet_encoding and transport != "xhttp":
        query.append(("packetEncoding", settings.packet_encoding))
    return query

async def get_xray_config() -> dict:
    try:
        async with aiofiles.open(settings.xray_config_path, "r") as f:
            content = await f.read()
            return json.loads(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {str(e)}")

async def save_xray_config(config: dict) -> None:
    try:
        async with aiofiles.open(settings.xray_config_path, "w") as f:
            await f.write(json.dumps(config, indent=2))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {str(e)}")

def reload_xray() -> None:
    """Hard restart fallback: HUP makes the entrypoint loop respawn Xray.

    Only used if the live API path fails — it drops all active sessions, so we
    avoid it for routine client add/remove.
    """
    try:
        subprocess.run(["pkill", "-HUP", "xray"], check=False)
    except Exception as e:
        print(f"Warning: Failed to reload xray: {e}")


def _api_server() -> str:
    return f"127.0.0.1:{settings.xray_api_port}"


async def _run_xray_api(args: list[str], stdin_file: str | None = None) -> tuple[int, str]:
    cmd = ["xray", "api", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def xray_api_add(inbound: dict, client_record: dict) -> bool:
    """Add a single client to a live inbound via `xray api adu` (no restart).

    adu parses the file as a full Xray config and merges users into the inbound
    whose tag matches, so we hand it a minimal inbound stub.
    """
    tag = inbound.get("tag")
    port = inbound.get("port")
    if not tag or not port:
        return False
    payload = {
        "inbounds": [
            {
                "tag": tag,
                "port": int(port),
                "protocol": "vless",
                "settings": {"clients": [client_record], "decryption": "none"},
            }
        ]
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        rc, out = await _run_xray_api(["adu", f"--server={_api_server()}", path])
        ok = rc == 0 and "Added 1 user" in out
        if not ok:
            print(f"xray adu failed (tag={tag}): {out.strip()}")
        return ok
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def xray_api_remove(tag: str, email: str) -> bool:
    rc, out = await _run_xray_api(["rmu", f"--server={_api_server()}", f"-tag={tag}", email])
    # rmu reports "not found" if the user was never live (e.g. after restart);
    # treat that as success since the goal state (absent) holds.
    ok = rc == 0 or "not found" in out.lower() or "Removed" in out
    if not ok:
        print(f"xray rmu failed (tag={tag}, email={email}): {out.strip()}")
    return ok


async def _online_users() -> list[str]:
    """Emails of users with at least one live session right now."""
    rc, out = await _run_xray_api(["statsgetallonlineusers", f"--server={_api_server()}"])
    if rc != 0:
        return []
    try:
        names = json.loads(out or "{}").get("users", []) or []
    except json.JSONDecodeError:
        return []
    emails = []
    for n in names:  # "user>>>EMAIL>>>online"
        parts = n.split(">>>")
        if len(parts) >= 3 and parts[0] == "user":
            emails.append(parts[1])
    return emails


async def _online_ips(email: str) -> dict[str, int]:
    """{source_ip: last_seen_unix} for a user's live sessions."""
    rc, out = await _run_xray_api(
        ["statsonlineiplist", f"--server={_api_server()}", "-email", email]
    )
    if rc != 0:
        return {}
    try:
        return json.loads(out or "{}").get("ips", {}) or {}
    except json.JSONDecodeError:
        return {}


async def enforce_conn_limit_once() -> int:
    """Block source IPs that exceed the per-subscription limit.

    For each online user with more than `conn_limit` distinct IPs, keep the
    `conn_limit` most-recently-seen IPs and block the rest. The block list is
    rebuilt every cycle with `sib -reset`, so an IP that stops being excess is
    automatically unblocked next cycle. Returns the count of blocked IPs.
    """
    limit = settings.conn_limit
    if limit <= 0:
        return 0
    excess: list[str] = []
    for email in await _online_users():
        ips = await _online_ips(email)
        if len(ips) <= limit:
            continue
        # keep the `limit` newest by timestamp, block the older overflow
        ordered = sorted(ips.items(), key=lambda kv: kv[1], reverse=True)
        excess.extend(ip for ip, _ in ordered[limit:])

    # rebuild the conn-limit block rule with the full current overflow set
    args = ["sib", f"--server={_api_server()}", "-outbound=block",
            "-ruletag=conn-limit", "-reset", *excess]
    rc, out = await _run_xray_api(args)
    if rc != 0:
        print(f"sib failed: {out.strip()}")
    elif excess:
        print(f"conn-limit: blocked {len(excess)} excess IP(s): {excess}")
    return len(excess)


async def _conn_limit_loop() -> None:
    interval = max(15, settings.conn_limit_interval)
    while True:
        try:
            await enforce_conn_limit_once()
        except Exception as exc:
            print(f"conn-limit loop error: {exc}")
        await asyncio.sleep(interval)

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != settings.agent_token:
        raise HTTPException(status_code=403, detail="Invalid token")
    return credentials.credentials

app = FastAPI(title="Aegis VPN Agent")


@app.on_event("startup")
async def _start_background_tasks() -> None:
    if settings.conn_limit > 0:
        asyncio.create_task(_conn_limit_loop())
        print(f"conn-limit enforcement on: max {settings.conn_limit} IPs/sub, "
              f"every {settings.conn_limit_interval}s")


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
async def bulk_add_clients(reqs: List[ClientAddRequest]):
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
    
    exists = False
    for inbound in config.get("inbounds", []):
        if inbound.get("protocol") == "vless":
            clients = inbound.get("settings", {}).get("clients", [])
            if any(c.get("id") == uuid for c in clients):
                exists = True
                break
                
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
    """Per-client traffic counters from Xray's StatsService.

    Returns cumulative byte counters (since the last Xray start) keyed by the
    client `email`. Counters reset to zero whenever Xray restarts, so the bot
    is responsible for delta accounting on its side.
    """
    api_server = f"127.0.0.1:{settings.xray_api_port}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "xray", "api", "statsquery", f"--server={api_server}", "user>>>",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="xray api statsquery timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"stats query failed: {e}")

    if proc.returncode != 0:
        detail = (err or b"").decode("utf-8", errors="replace")[:200]
        raise HTTPException(status_code=500, detail=f"xray api error: {detail}")

    try:
        data = json.loads(out.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        data = {}

    # stat names look like: user>>>{email}>>>traffic>>>uplink|downlink
    by_email: dict[str, dict[str, int]] = {}
    for stat in data.get("stat", []) or []:
        parts = (stat.get("name") or "").split(">>>")
        if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
            email, direction = parts[1], parts[3]
            if direction not in ("uplink", "downlink"):
                continue
            value = int(stat.get("value", 0) or 0)
            rec = by_email.setdefault(email, {"uplink": 0, "downlink": 0})
            rec[direction] = value
    return {"stats": by_email}


@app.get("/sub-fast/{token}", dependencies=[Depends(verify_token)])
async def get_fast_subscription(token: str):
    uuid = token
    async with config_lock:
        config = await get_xray_config()

    exists = False
    for inbound in list_vless_inbounds(config):
        clients = inbound.get("settings", {}).get("clients", [])
        if any(c.get("id") == uuid for c in clients):
            exists = True
            break

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
