"""Read-only sample of distinct direct VLESS sources. Never export raw addresses."""

import asyncio
import hashlib
import hmac
import ipaddress
import re
import time
from pathlib import Path

from . import connlimit

# Optional, dedicated fleet-wide 32-byte key (64 hex digits). Never derive from
# user credentials, which would permit offline enumeration by a subscriber.
KEY_FILE = Path("/data/node-control/subscription-ip-hmac.key")
IDENTITY = re.compile(r"user_[1-9][0-9]*_sub_([1-9][0-9]*)(?:_dev_[1-9][0-9]*)?")
MAX_IDENTITIES = 512
MAX_TOKENS = 4096


async def sample() -> dict | None:
    try:
        key = bytes.fromhex(KEY_FILE.read_text("ascii").strip())
        if len(key) != 32:
            return None
        started = int(time.time())
        async with asyncio.timeout(8):
            # Keep raw identity suffixes: a CDN email is not a direct client.
            identities = {email: m[1] for email in await connlimit.online_users() if (m := IDENTITY.fullmatch(email))}
            if len(identities) > MAX_IDENTITIES:
                return None
            buckets: dict[str, set[str]] = {}
            edges = connlimit.cdn_peer_ips()
            tokens = 0
            names = list(identities)
            for start in range(0, len(names), 4):
                batch = names[start : start + 4]
                results = await asyncio.gather(*(connlimit.online_ips(name) for name in batch), return_exceptions=True)
                for name, result in zip(batch, results, strict=True):
                    if not isinstance(result, dict):
                        return None
                    sid = identities[name]
                    bucket = buckets.setdefault(sid, set())
                    for raw in result:
                        addr = ipaddress.ip_address(raw)
                        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                            addr = addr.ipv4_mapped
                        if not addr.is_global or addr.is_multicast or str(addr) in edges:
                            continue
                        # Same IP compares equal across nodes but not across subscriptions.
                        tag = hmac.digest(key, b"v1:" + sid.encode() + b":" + addr.packed, "sha256")[:16].hex()
                        if tag not in bucket:
                            bucket.add(tag)
                            tokens += 1
                            if tokens > MAX_TOKENS:
                                return None
        return {
            "sampled_at": started,
            "key_id": hashlib.sha256(key).hexdigest()[:16],
            "subscriptions": {sid: sorted(tags) for sid, tags in buckets.items() if tags},
        }
    except (OSError, ValueError, connlimit.StatsQueryError, TimeoutError):
        # Optional measurement must not suppress regular telemetry or ACKs.
        return None
