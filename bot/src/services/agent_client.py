from typing import Any, Dict, List

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

_default_timeout = aiohttp.ClientTimeout(total=10, connect=3, sock_read=8)
_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=300)
        _session = aiohttp.ClientSession(connector=connector, timeout=_default_timeout)
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


class AgentClient:
    def __init__(self, agent_url: str, agent_token: str):
        self.base_url = agent_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {agent_token}"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def add_client(self, uuid: str, email: str, expire_ms: int = 0) -> bool:
        payload = {"uuid": uuid, "email": email, "expire_ms": expire_ms}
        async with get_session().post(
            f"{self.base_url}/client/add", json=payload, headers=self.headers
        ) as resp:
            if resp.status == 200:
                return True
            resp.raise_for_status()
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def remove_client(self, uuid: str) -> bool:
        payload = {"uuid": uuid}
        async with get_session().post(
            f"{self.base_url}/client/remove", json=payload, headers=self.headers
        ) as resp:
            if resp.status == 200:
                return True
            resp.raise_for_status()
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def bulk_add(self, clients: List[Dict[str, Any]]) -> bool:
        async with get_session().post(
            f"{self.base_url}/client/bulk",
            json=clients,
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 200:
                return True
            resp.raise_for_status()
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def get_health(self) -> dict:
        async with get_session().get(f"{self.base_url}/health") as resp:
            if resp.status == 200:
                return await resp.json()
            resp.raise_for_status()
            return {}

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=4))
    async def get_stats(self) -> Dict[str, Dict[str, int]]:
        """Per-email traffic counters from the node's Xray.

        Returns ``{email: {"uplink": int, "downlink": int}}``. Counters are
        cumulative since the node's Xray last started.
        """
        async with get_session().get(
            f"{self.base_url}/stats",
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                payload = await resp.json()
                return payload.get("stats", {}) or {}
            resp.raise_for_status()
            return {}

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=1))
    async def get_subscription(self, token: str, profile: str = "safe") -> str:
        path = "sub-fast" if profile == "fast" else "sub"
        timeout = aiohttp.ClientTimeout(total=8, connect=3, sock_read=5)
        async with get_session().get(
            f"{self.base_url}/{path}/{token}", headers=self.headers, timeout=timeout
        ) as resp:
            if resp.status == 200:
                return await resp.text()
            resp.raise_for_status()
            return ""
