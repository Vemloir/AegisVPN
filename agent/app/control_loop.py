import asyncio
import os
import random
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Literal

from . import hysteria
from .config import settings
from .control_client import ControlClient
from .control_models import DesiredSnapshot
from .reconcile import (
    ReconcileResult,
    load_applied_state,
    reconcile_snapshot,
)
from .xray import (
    build_subscription_query,
    find_vless_inbound,
    get_online_emails,
    get_xray_config,
    query_traffic_stats,
)

_TELEMETRY_SEQUENCE_PATH = "/data/node-control/telemetry-sequence"

Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[float], float]
TelemetryBuilder = Callable[[ReconcileResult | None], Awaitable[dict]]


def _load_telemetry_sequence() -> int:
    try:
        with open(_TELEMETRY_SEQUENCE_PATH) as file:
            return max(0, int(file.read().strip()))
    except (OSError, ValueError):
        return 0


def _save_telemetry_sequence(sequence: int) -> None:
    directory = os.path.dirname(_TELEMETRY_SEQUENCE_PATH) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix=".telemetry-sequence-",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w") as file:
            file.write(str(sequence))
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, _TELEMETRY_SEQUENCE_PATH)
    finally:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass


async def _build_telemetry(result: ReconcileResult | None) -> dict:
    online = set(await get_online_emails())
    online.update(await hysteria.online())
    stats = await query_traffic_stats()
    for email, counters in (await hysteria.traffic()).items():
        bucket = stats.setdefault(email, {"uplink": 0, "downlink": 0})
        bucket["uplink"] += int(counters.get("uplink", 0) or 0)
        bucket["downlink"] += int(counters.get("downlink", 0) or 0)
    state = load_applied_state()
    config = await get_xray_config()
    subscription_templates: list[dict] = []
    for profile, preferred_network in (("safe", "xhttp"), ("fast", "tcp")):
        inbound = find_vless_inbound(config, preferred_network=preferred_network)
        if not inbound:
            continue
        host = (
            settings.fast_host_ip
            if profile == "fast" and settings.fast_host_ip
            else settings.host_ip
        )
        port = int(
            inbound.get("port")
            or (
                settings.xray_tcp_port
                if profile == "fast"
                else settings.xray_port
            )
            or settings.xray_port
        )
        subscription_templates.append(
            {
                "profile": profile,
                "host": host,
                "port": port,
                "query": [
                    [key, value]
                    for key, value in build_subscription_query(inbound)
                ],
            }
        )
    return {
        "applied_generation": state.generation,
        "applied_digest": state.digest,
        "online_emails": sorted(online),
        "stats": stats,
        "subscription_templates": subscription_templates,
        "reconciliation": asdict(result) if result is not None else None,
    }


async def _reconcile_cached_state(mode: str) -> ReconcileResult | None:
    if mode != "apply":
        return None
    applied = load_applied_state()
    if applied.generation < 1 or not applied.digest:
        return None
    return await reconcile_snapshot(
        DesiredSnapshot(
            schema_version=applied.schema_version,
            generation=applied.generation,
            digest=applied.digest,
            items=applied.items,
        ),
        observe=False,
    )


async def control_loop(
    stop_event: asyncio.Event | None = None,
    *,
    client: ControlClient | None = None,
    mode: Literal["observe", "apply"] | None = None,
    sleep: Sleep = asyncio.sleep,
    jitter: Jitter | None = None,
    telemetry_builder: TelemetryBuilder = _build_telemetry,
) -> None:
    effective_mode = mode or settings.control_mode
    if effective_mode not in {"observe", "apply"}:
        return
    stop = stop_event or asyncio.Event()
    control_client = client or ControlClient.from_settings()
    random_delay = jitter or (lambda maximum: random.uniform(0, maximum))
    backoff = 1.0
    sequence = _load_telemetry_sequence()

    try:
        while not stop.is_set():
            result: ReconcileResult | None = None
            try:
                applied = load_applied_state()
                snapshot = await control_client.sync(applied)
                if snapshot is not None:
                    result = await reconcile_snapshot(
                        snapshot,
                        observe=effective_mode == "observe",
                    )
                    if effective_mode == "apply" and result.success:
                        await control_client.ack(
                            generation=snapshot.generation,
                            digest=snapshot.digest,
                            success=True,
                            error=None,
                        )
                else:
                    result = await _reconcile_cached_state(effective_mode)

                payload = await telemetry_builder(result)
                await control_client.send_telemetry(
                    sequence=sequence + 1,
                    payload=payload,
                )
                sequence += 1
                _save_telemetry_sequence(sequence)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never include exception text: HTTP libraries can embed URLs,
                # headers or response bodies containing node credentials/UUIDs.
                print(f"control loop error: {type(exc).__name__}")
                try:
                    await _reconcile_cached_state(effective_mode)
                except Exception as cached_exc:
                    print(
                        "cached control reconciliation error: "
                        f"{type(cached_exc).__name__}"
                    )
                if stop.is_set():
                    break
                await sleep(max(0.0, random_delay(backoff)))
                backoff = min(60.0, backoff * 2)
    finally:
        await control_client.close()


def start_control_task() -> asyncio.Task | None:
    if settings.control_mode == "off":
        return None
    return asyncio.create_task(
        control_loop(mode=settings.control_mode),
        name="agent-control-loop",
    )
