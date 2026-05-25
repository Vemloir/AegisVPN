"""Publish the privacy policy as a Telegraph page so it opens as a clean
in-app page (Instant View) via a URL button instead of a chat message."""
import hashlib
import json
import re
from pathlib import Path

import aiohttp

from src.core.logger import logger

_API = "https://api.telegra.ph"
_CACHE = Path("/data/telegraph.json")  # {token, url_ru, url_en}


async def _api(method: str, **data) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{_API}/{method}", json=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
            j = await r.json()
            if not j.get("ok"):
                raise RuntimeError(j.get("error", "telegraph error"))
            return j["result"]


def _load() -> dict:
    try:
        return json.loads(_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        _CACHE.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _to_nodes(text: str) -> list:
    """Convert our Telegram-HTML-ish privacy text into Telegraph DOM nodes."""
    nodes: list = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        heading = re.fullmatch(r"<b>(.*?)</b>", line)
        if heading:
            nodes.append({"tag": "h4", "children": [heading.group(1)]})
            continue
        children: list = []
        pos = 0
        for m in re.finditer(r"<b>(.*?)</b>", line):
            if m.start() > pos:
                children.append(line[pos:m.start()])
            children.append({"tag": "strong", "children": [m.group(1)]})
            pos = m.end()
        if pos < len(line):
            children.append(line[pos:])
        nodes.append({"tag": "p", "children": children or [line]})
    return nodes


async def get_privacy_url(language: str, html_text: str, title: str) -> str | None:
    """Return a Telegraph URL for the policy. Creates the page once, and edits
    it in place when the text changes (tracked by hash), so the page always
    reflects the current policy. Returns None if Telegraph is unreachable
    (caller falls back to a text message)."""
    cache = _load()
    url_key, path_key, hash_key = f"url_{language}", f"path_{language}", f"hash_{language}"
    digest = hashlib.sha256(html_text.encode("utf-8")).hexdigest()[:16]

    if cache.get(url_key) and cache.get(hash_key) == digest:
        return cache[url_key]

    try:
        if not cache.get("token"):
            acc = await _api("createAccount", short_name="AegisVPN", author_name="AegisVPN")
            cache["token"] = acc["access_token"]
            _save(cache)

        nodes = _to_nodes(html_text)
        path = cache.get(path_key)
        if path:
            page = await _api(
                f"editPage/{path}",
                access_token=cache["token"],
                title=title[:256],
                content=nodes,
                author_name="AegisVPN",
            )
        else:
            page = await _api(
                "createPage",
                access_token=cache["token"],
                title=title[:256],
                content=nodes,
                author_name="AegisVPN",
            )
        cache[url_key] = page["url"]
        cache[path_key] = page["path"]
        cache[hash_key] = digest
        _save(cache)
        return cache[url_key]
    except Exception as exc:
        logger.warning("telegraph publish failed: %s", exc)
        return cache.get(url_key)  # stale URL is better than nothing
