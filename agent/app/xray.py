"""Xray control plane: config I/O, live client add/remove, subscription URIs,
and traffic stats — everything that talks to the local Xray process or its API.
"""

import asyncio
import json
import os
import subprocess
import tempfile

import aiofiles
from fastapi import HTTPException

from .config import settings

# Guards read-modify-write cycles on the on-disk Xray config.
config_lock = asyncio.Lock()


# --- inbound helpers ---------------------------------------------------------


def list_vless_inbounds(config: dict) -> list[dict]:
    return [inbound for inbound in config.get("inbounds", []) if inbound.get("protocol") == "vless"]


def get_transport_type(inbound: dict) -> str:
    stream = inbound.get("streamSettings", {})
    network = (stream.get("network") or "tcp").lower()
    if network == "grpc":
        return "grpc"
    return "xhttp" if network == "xhttp" else "tcp"


def find_vless_inbound(config: dict, preferred_network: str | None = None) -> dict:
    inbounds = list_vless_inbounds(config)
    if preferred_network:
        preferred_network = preferred_network.lower()
        for inbound in inbounds:
            if get_transport_type(inbound) == preferred_network:
                return inbound
    return inbounds[0] if inbounds else {}


def build_client_record(uuid: str, email: str, inbound: dict) -> dict:
    # tcp/REALITY clients carry the vision flow (xtls-rprx-vision); xhttp/grpc
    # stay flow-less. Greece shares ONE reality keypair across inbounds — only
    # the per-client flow differs, set per the inbound the client lands on.
    record = {
        "id": uuid,
        "email": email,
    }
    if get_transport_type(inbound) == "tcp":
        record["flow"] = "xtls-rprx-vision"
    return record


def build_subscription_query(inbound: dict) -> list[tuple[str, str]]:
    stream = inbound.get("streamSettings", {})
    reality = stream.get("realitySettings", {})
    transport = get_transport_type(inbound)
    # All transports (xhttp/tcp/grpc) share the SINGLE reality keypair now.
    public_key = settings.public_key
    short_id = settings.short_id
    fingerprint = settings.fingerprint
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
    elif transport == "grpc":
        grpc_settings = stream.get("grpcSettings", {})
        query.append(("serviceName", grpc_settings.get("serviceName") or settings.grpc_service_name))
        # "gun" = single gRPC stream (vs "multi"); standard v2ray grpc link param.
        query.append(("mode", "gun"))
    else:  # tcp/REALITY with the vision flow (xtls-rprx-vision)
        query.append(("headerType", "none"))
        query.append(("flow", "xtls-rprx-vision"))
    if settings.packet_encoding:
        query.append(("packetEncoding", settings.packet_encoding))
    return query


# --- config I/O --------------------------------------------------------------


async def get_xray_config() -> dict:
    try:
        async with aiofiles.open(settings.xray_config_path) as f:
            content = await f.read()
            return json.loads(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {str(e)}") from e


async def save_xray_config(config: dict) -> None:
    try:
        async with aiofiles.open(settings.xray_config_path, "w") as f:
            await f.write(json.dumps(config, indent=2))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {str(e)}") from e


def reload_xray() -> None:
    """Hard restart fallback: HUP makes the entrypoint loop respawn Xray.

    Only used if the live API path fails — it drops all active sessions, so we
    avoid it for routine client add/remove.
    """
    try:
        subprocess.run(["pkill", "-HUP", "xray"], check=False)
    except Exception as e:
        print(f"Warning: Failed to reload xray: {e}")


# --- xray api ----------------------------------------------------------------


def api_server() -> str:
    return f"127.0.0.1:{settings.xray_api_port}"


async def run_xray_api(args: list[str]) -> tuple[int, str]:
    cmd = ["xray", "api", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
    except TimeoutError:
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
        rc, out = await run_xray_api(["adu", f"--server={api_server()}", path])
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
    rc, out = await run_xray_api(["rmu", f"--server={api_server()}", f"-tag={tag}", email])
    # rmu reports "not found" if the user was never live (e.g. after restart);
    # treat that as success since the goal state (absent) holds.
    ok = rc == 0 or "not found" in out.lower() or "Removed" in out
    if not ok:
        print(f"xray rmu failed (tag={tag}, email={email}): {out.strip()}")
    return ok


def _parse_online_users(raw: bytes) -> list[str]:
    """Extract the list of online user emails from `statsgetallonlineusers` output.

    Xray reports `{"users": {<email>: <ip-list-or-count>, ...}}`. We only need the
    keys (the emails with at least one live session). We also tolerate a list shape
    (of plain emails or `{"email": ...}` records) across Xray versions.
    """
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, TypeError):
        return []
    users = data.get("users")
    if isinstance(users, dict):
        return [str(k) for k in users.keys()]
    if isinstance(users, list):
        emails: list[str] = []
        for item in users:
            if isinstance(item, str):
                emails.append(item)
            elif isinstance(item, dict):
                email = item.get("email") or item.get("user") or item.get("name")
                if email:
                    emails.append(str(email))
        return emails
    return []


async def get_online_emails() -> list[str]:
    """Emails of users with at least one active session right now.

    Authoritative live state from Xray's online stats (requires
    `statsUserOnline: true` in policy). Used by the bot to decide, per device,
    whether it is connected — no traffic-delta guessing.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "xray", "api", "statsgetallonlineusers", f"--server={api_server()}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (TimeoutError, Exception):
        return []
    return _parse_online_users(out)


async def get_online_count() -> int:
    """Number of users with active sessions right now (xray statsgetallonlineusers)."""
    return len(await get_online_emails())


async def query_traffic_stats() -> dict[str, dict[str, int]]:
    """Per-client cumulative byte counters keyed by email (since last Xray start).

    Counters reset to zero whenever Xray restarts, so the bot is responsible for
    delta accounting on its side.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "xray",
            "api",
            "statsquery",
            f"--server={api_server()}",
            "user>>>",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        raise HTTPException(status_code=504, detail="xray api statsquery timed out") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"stats query failed: {e}") from e

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
    return by_email
