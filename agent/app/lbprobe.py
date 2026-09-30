"""Load probe for the client-side "Автовыбор" balancer.

The subscription's auto-select entry is an xray leastPing balancer: every
client health-checks each node through the tunnel and takes the one that
answers fastest. Xray ranks on nothing but that delay, so this probe folds the
node's own state into it. Xray routes the probe hostname to this listener,
and the answer is held back by

    load penalty      base * rho / (1 - rho)   (M/M/1 queueing delay)
    switch penalty    added unless this user is already active here

so what the client ranks is ping + load penalty + switch penalty. At or past
full load the probe does not answer at all and the client counts the node as
dead. rho is the busier of CPU and link utilisation.

The switch penalty keeps a user on the node they are on while it stays
competitive: xray never moves an open connection, but it picks a node for
every new one, and flapping between two near-equal nodes changes the user's
exit IP mid-session. A user with no traffic anywhere pays it on every node,
which ranks nodes exactly as before.

The path is /p/<id>, id = first 16 hex of sha256(client UUID). The probe's
own few bytes never make a user "active": activity means real traffic moved.
"""

import asyncio
import hashlib
import time

from .config import settings
from .xray import get_xray_config, query_traffic_stats

_SAMPLE_SECONDS = 2.0
_SMOOTHING_SECONDS = 30.0
_ACTIVITY_SECONDS = 15.0
_ACTIVE_WINDOW_SECONDS = 60.0
_ACTIVE_MIN_BYTES = 64 * 1024
_REQUEST_TIMEOUT = 2.0
_MAX_CONCURRENT = 256
_SKIP_INTERFACES = ("lo", "docker", "veth", "br-", "wg", "warp", "tun")


def _read_cpu() -> tuple[int, int]:
    with open("/proc/stat") as fh:
        fields = [int(x) for x in fh.readline().split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return idle, sum(fields)


def _read_net_bytes() -> int:
    """Busiest direction summed over physical interfaces. A VPN node forwards
    what it receives, so rx and tx move together; the larger one is the one
    that hits the link first."""
    rx = tx = 0
    with open("/proc/net/dev") as fh:
        for line in fh.readlines()[2:]:
            name, _, data = line.partition(":")
            if name.strip().startswith(_SKIP_INTERFACES):
                continue
            cols = data.split()
            rx += int(cols[0])
            tx += int(cols[8])
    return max(rx, tx)


def penalty_ms(rho: float) -> float | None:
    """Delay to add for utilisation rho, or None when the node is full."""
    if rho >= settings.lb_full_load:
        return None
    rho = max(0.0, rho)
    return settings.lb_load_base_ms * rho / (1.0 - rho)


class LoadProbe:
    def __init__(self) -> None:
        self.rho = 0.0
        self.cpu = 0.0
        self.net = 0.0
        self._active: set[str] = set()
        self._probe_ids: dict[str, str] = {}
        self._history: dict[str, list[tuple[float, int]]] = {}
        self._slots = asyncio.Semaphore(_MAX_CONCURRENT)

    # --- load ---------------------------------------------------------------

    async def sample_loop(self) -> None:
        idle0, total0 = _read_cpu()
        net0, t0 = _read_net_bytes(), time.monotonic()
        alpha = _SAMPLE_SECONDS / _SMOOTHING_SECONDS
        while True:
            await asyncio.sleep(_SAMPLE_SECONDS)
            try:
                idle1, total1 = _read_cpu()
                net1, t1 = _read_net_bytes(), time.monotonic()
            except (OSError, ValueError, IndexError):
                continue
            busy = 1.0 - (idle1 - idle0) / max(1, total1 - total0)
            self.cpu += alpha * (min(1.0, max(0.0, busy)) - self.cpu)
            if settings.lb_link_mbps > 0:
                mbps = (net1 - net0) * 8 / max(1e-3, t1 - t0) / 1e6
                self.net += alpha * (min(1.0, mbps / settings.lb_link_mbps) - self.net)
            self.rho = max(self.cpu, self.net)
            idle0, total0, net0, t0 = idle1, total1, net1, t1

    # --- who is on this node ------------------------------------------------

    async def activity_loop(self) -> None:
        while True:
            try:
                await self._refresh_activity()
            except Exception as exc:  # keep probing on stale data
                print(f"lb-probe: activity refresh failed: {exc}")
            await asyncio.sleep(_ACTIVITY_SECONDS)

    async def _refresh_activity(self) -> None:
        config = await get_xray_config()
        ids: dict[str, str] = {}
        for inbound in config.get("inbounds", []):
            for client in (inbound.get("settings") or {}).get("clients", []) or []:
                uuid, email = client.get("id"), client.get("email")
                if uuid and email:
                    ids[email] = hashlib.sha256(uuid.encode()).hexdigest()[:16]
        self._probe_ids = ids

        now = time.monotonic()
        active: set[str] = set()
        for email, counters in (await query_traffic_stats()).items():
            total = counters.get("uplink", 0) + counters.get("downlink", 0)
            history = self._history.setdefault(email, [])
            history.append((now, total))
            while history and now - history[0][0] > _ACTIVE_WINDOW_SECONDS:
                history.pop(0)
            if total - history[0][1] >= _ACTIVE_MIN_BYTES and email in ids:
                active.add(ids[email])
        self._active = active

    # --- the probe itself ---------------------------------------------------

    def delay_ms(self, probe_id: str) -> float | None:
        load = penalty_ms(self.rho)
        if load is None:
            return None
        switch = 0.0 if probe_id in self._active else settings.lb_switch_penalty_ms
        return load + switch

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self._slots:
            try:
                line = await asyncio.wait_for(reader.readline(), _REQUEST_TIMEOUT)
                parts = line.decode("latin-1", errors="replace").split()
                path = parts[1] if len(parts) >= 2 else ""
                probe_id = path[3:19] if path.startswith("/p/") else ""
                delay = self.delay_ms(probe_id)
                if delay is None:
                    return  # full: no answer, the client marks this node dead
                await asyncio.sleep(delay / 1000.0)
                writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except (TimeoutError, ConnectionError):
                pass
            finally:
                writer.close()


probe = LoadProbe()


async def start_lb_probe() -> None:
    if settings.lb_probe_port <= 0:
        return
    server = await asyncio.start_server(probe.handle, "127.0.0.1", settings.lb_probe_port)
    asyncio.create_task(probe.sample_loop(), name="lb-probe-load")
    asyncio.create_task(probe.activity_loop(), name="lb-probe-activity")
    asyncio.create_task(server.serve_forever(), name="lb-probe-server")
    print(
        f"lb-probe on 127.0.0.1:{settings.lb_probe_port}: "
        f"base {settings.lb_load_base_ms}ms, switch {settings.lb_switch_penalty_ms}ms, "
        f"full at {settings.lb_full_load:.0%}, link {settings.lb_link_mbps or 'n/a'} Mbps"
    )
