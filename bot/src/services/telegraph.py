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
    # Serialize ourselves with ensure_ascii=False: aiohttp's json= encoder
    # defaults to ensure_ascii=True, which escapes every Cyrillic char to a
    # 6-byte \uXXXX sequence and inflates a ~60 KB ToS past Telegraph's content
    # limit (CONTENT_TOO_BIG). Compact UTF-8 keeps it well under the cap.
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{_API}/{method}", data=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
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


def _inline_children(line: str) -> list:
    """Render inline bold spans. Accepts both Telegram-HTML <b>..</b> and
    Markdown **..** so the privacy policy (HTML-ish) and the ToS (Markdown)
    can share one converter."""
    # Normalise Markdown bold to the HTML form we already parse.
    line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
    children: list = []
    pos = 0
    for m in re.finditer(r"<b>(.*?)</b>", line):
        if m.start() > pos:
            children.append(line[pos : m.start()])
        children.append({"tag": "strong", "children": [m.group(1)]})
        pos = m.end()
    if pos < len(line):
        children.append(line[pos:])
    return children or [line]


def _to_nodes(text: str) -> list:
    """Convert our document text into Telegraph DOM nodes.

    Supports the Telegram-HTML-ish privacy format (``<b>..</b>`` lines, ``•``
    bullets) and the Markdown ToS format (``#``/``##``/``###`` headings,
    ``**bold**``, ``-`` bullets, ``---`` dividers, ``> quote``)."""
    nodes: list = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        # Markdown horizontal rule -> Telegraph <hr>.
        if re.fullmatch(r"-{3,}", line):
            nodes.append({"tag": "hr"})
            continue
        # Markdown ATX headings (#, ##, ###) -> h3/h4.
        md_heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if md_heading:
            tag = "h3" if len(md_heading.group(1)) <= 2 else "h4"
            nodes.append({"tag": tag, "children": _inline_children(md_heading.group(2))})
            continue
        # A whole line wrapped in <b> is treated as a heading (privacy format).
        whole_bold = re.fullmatch(r"<b>(.*?)</b>", line)
        if whole_bold:
            nodes.append({"tag": "h4", "children": [whole_bold.group(1)]})
            continue
        # Markdown blockquote.
        if line.startswith(">"):
            nodes.append({"tag": "blockquote", "children": _inline_children(line[1:].strip())})
            continue
        # List items: Markdown "- " / "* " or our "• " bullets.
        bullet = re.match(r"^([-*]|•)\s+(.*)$", line)
        if bullet:
            nodes.append({"tag": "p", "children": _inline_children(f"• {bullet.group(2)}")})
            continue
        nodes.append({"tag": "p", "children": _inline_children(line)})
    return nodes


# Telegraph rejects pages whose serialized content is too large (CONTENT_TOO_BIG)
# at roughly ~16 KB of JSON, far below the documented 64 KB. Long documents (the
# ToS is ~60 KB of text) are therefore split across several chained pages.
_MAX_CONTENT_CHARS = 14000


def _chunk_nodes(nodes: list) -> list[list]:
    """Split DOM nodes into chunks each under the Telegraph content budget,
    never splitting a single node. Returns at least one chunk."""
    chunks: list[list] = []
    current: list = []
    size = 2  # the surrounding "[]"
    for node in nodes:
        node_size = len(json.dumps(node, ensure_ascii=False)) + 1  # + comma
        if current and size + node_size > _MAX_CONTENT_CHARS:
            chunks.append(current)
            current = []
            size = 2
        current.append(node)
        size += node_size
    chunks.append(current)
    return chunks


async def _put_page(token: str, path: str | None, title: str, content: list) -> dict:
    """Create the page, or edit it in place if we already have its path."""
    if path:
        return await _api(
            f"editPage/{path}", access_token=token, title=title[:256], content=content, author_name="AegisVPN"
        )
    return await _api("createPage", access_token=token, title=title[:256], content=content, author_name="AegisVPN")


async def _get_page_url(slug: str, language: str, html_text: str, title: str) -> str | None:
    """Return a Telegraph URL for a document ``slug`` (e.g. ``privacy``/``tos``).

    Creates the page(s) once and edits in place when the text changes (tracked
    by hash), so the page always reflects the current document. Oversized
    documents are split across several Telegraph pages chained with a "next"
    link; the URL of the first page is returned. Returns None / a stale URL if
    Telegraph is unreachable (caller falls back to a text message)."""
    cache = _load()
    url_key = f"url_{slug}_{language}"
    hash_key = f"hash_{slug}_{language}"
    digest = hashlib.sha256(html_text.encode("utf-8")).hexdigest()[:16]

    if cache.get(url_key) and cache.get(hash_key) == digest:
        return cache[url_key]

    try:
        if not cache.get("token"):
            acc = await _api("createAccount", short_name="AegisVPN", author_name="AegisVPN")
            cache["token"] = acc["access_token"]
            _save(cache)
        token = cache["token"]

        chunks = _chunk_nodes(_to_nodes(html_text))
        total = len(chunks)
        next_label = "Далее →" if language == "ru" else "Next →"

        # Publish back-to-front so each page can embed a link to the next one.
        next_url: str | None = None
        first_url: str | None = None
        for i in range(total - 1, -1, -1):
            content = list(chunks[i])
            if next_url is not None:
                content.append(
                    {"tag": "p", "children": [{"tag": "a", "attrs": {"href": next_url}, "children": [next_label]}]}
                )
            page_title = title if total == 1 else f"{title} ({i + 1}/{total})"
            path_key = f"path_{slug}_{language}_{i}"
            page = await _put_page(token, cache.get(path_key), page_title, content)
            cache[path_key] = page["path"]
            next_url = page["url"]
            if i == 0:
                first_url = page["url"]

        cache[url_key] = first_url
        cache[hash_key] = digest
        _save(cache)
        return first_url
    except Exception as exc:
        logger.warning("telegraph publish failed: %s", exc)
        return cache.get(url_key)  # stale URL is better than nothing


async def get_privacy_url(language: str, html_text: str, title: str) -> str | None:
    """Telegraph URL for the privacy policy (see :func:`_get_page_url`)."""
    return await _get_page_url("privacy", language, html_text, title)


async def get_tos_url(language: str, html_text: str, title: str) -> str | None:
    """Telegraph URL for the Terms of Service / public offer."""
    return await _get_page_url("tos", language, html_text, title)
