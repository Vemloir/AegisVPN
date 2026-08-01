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
    # stay flow-less. A node shares ONE reality keypair across inbounds — only
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
        content = json.dumps(config, indent=2)
        await asyncio.to_thread(
            _atomic_write_text,
            settings.xray_config_path,
            content,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {str(e)}") from e


def _atomic_write_text(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, temporary_path = tempfile.mkstemp(
        prefix=".xray-config-",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass


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
    returncode, out, _ = await _run_process(
        cmd,
        timeout=8,
        stderr_to_stdout=True,
    )
    if returncode == 124:
        return returncode, "timeout"
    return returncode, out.decode("utf-8", "replace")


async def _run_process(
    command: list[str],
    *,
    timeout: float,
    stderr_to_stdout: bool = False,
) -> tuple[int, bytes, bytes]:
    """Run one child with a deadline and always reap it after termination.

    Return code 124 is reserved for a local timeout, matching the conventional
    ``timeout(1)`` status. The bytes returned after a timeout come from the
    mandatory post-kill ``communicate()`` call and are useful for bounded local
    diagnostics without leaving a zombie process behind.
    """
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=(asyncio.subprocess.STDOUT if stderr_to_stdout else asyncio.subprocess.PIPE),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return 124, out or b"", err or b""
    return proc.returncode or 0, out or b"", err or b""


async def wait_for_xray_ready(
    *,
    attempts: int = 20,
    delay_seconds: float = 0.25,
) -> bool:
    """Wait until the post-reload process answers its local API.

    A successful response proves that the replacement Xray process has parsed
    the atomically persisted desired config. Reconciliation must not acknowledge
    the generation while the reload outcome remains unknown.
    """
    for attempt in range(max(1, attempts)):
        try:
            return_code, _ = await run_xray_api(["statsquery", f"--server={api_server()}", "user>>>"])
        except (OSError, RuntimeError):
            return_code = 1
        if return_code == 0:
            return True
        if attempt + 1 < attempts:
            await asyncio.sleep(delay_seconds)
    return False


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


def normalize_online_name(value: str) -> str | None:
    """Return an email from Xray's online-stat name or a plain email.

    The pinned Xray emits ``user>>>EMAIL>>>online``. Older/newer compatible
    outputs may already contain the plain email. Delimiter-bearing values with
    any other shape are stat names for a different metric and must not leak into
    telemetry as device identities.
    """
    if ">>>" not in value:
        return value or None
    parts = value.split(">>>")
    if len(parts) == 3 and parts[0] == "user" and parts[2] == "online":
        return parts[1] or None
    return None


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
        return [email for key in users if (email := normalize_online_name(str(key)))]
    if isinstance(users, list):
        emails: list[str] = []
        for item in users:
            if isinstance(item, str):
                email = normalize_online_name(item)
                if email:
                    emails.append(email)
            elif isinstance(item, dict):
                value = item.get("email") or item.get("user") or item.get("name")
                if value and (email := normalize_online_name(str(value))):
                    emails.append(email)
        return emails
    return []


async def get_online_emails() -> list[str]:
    """Emails of users with at least one active session right now.

    Authoritative live state from Xray's online stats (requires
    `statsUserOnline: true` in policy). Used by the bot to decide, per device,
    whether it is connected — no traffic-delta guessing.
    """
    try:
        returncode, out, _ = await _run_process(
            [
                "xray",
                "api",
                "statsgetallonlineusers",
                f"--server={api_server()}",
            ],
            timeout=5,
        )
    except Exception:
        return []
    if returncode != 0:
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
        returncode, out, err = await _run_process(
            [
                "xray",
                "api",
                "statsquery",
                f"--server={api_server()}",
                "user>>>",
            ],
            timeout=10,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"stats query failed: {e}") from e

    if returncode == 124:
        raise HTTPException(status_code=504, detail="xray api statsquery timed out")
    if returncode != 0:
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
