"""Offline GeoIP: resolve an approximate city/country from an IP, fully locally.

Uses the free DB-IP City Lite database (CC-BY). The .mmdb is downloaded once into
the persistent /data volume and refreshed monthly — the user's IP is never sent to
any third party; we only fetch the public database file itself.

Lookups are best-effort: if the database is missing or an address isn't found, the
caller simply gets no location.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import time
from datetime import UTC, datetime

import aiohttp

from src.core.config import settings
from src.core.logger import logger

try:
    import geoip2.database
    import geoip2.errors

    _HAVE_GEOIP = True
except Exception:  # pragma: no cover - dependency optional at import time
    _HAVE_GEOIP = False

_DOWNLOAD_URL = "https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz"
_REFRESH_AFTER_DAYS = 40

_reader = None
_reader_mtime: float = 0.0


def _months_to_try() -> list[str]:
    """Current month first, then the previous one (the new file lands a few days late)."""
    now = datetime.now(UTC)
    cur = f"{now.year:04d}-{now.month:02d}"
    if now.month == 1:
        prev = f"{now.year - 1:04d}-12"
    else:
        prev = f"{now.year:04d}-{now.month - 1:02d}"
    return [cur, prev]


def _is_fresh(path: str) -> bool:
    try:
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        return age_days < _REFRESH_AFTER_DAYS and os.path.getsize(path) > 0
    except OSError:
        return False


async def ensure_db() -> None:
    """Make sure a reasonably fresh GeoIP database exists at the configured path.

    Best-effort and safe to call on every boot: a fresh local file is kept as-is;
    otherwise the latest monthly release is downloaded and gunzipped into place.
    """
    if not settings.geoip_enabled or not _HAVE_GEOIP:
        return
    path = settings.geoip_db_path
    if _is_fresh(path):
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_gz = path + ".tmp.gz"
    tmp_out = path + ".tmp"
    timeout = aiohttp.ClientTimeout(total=300, connect=20)
    for ym in _months_to_try():
        url = _DOWNLOAD_URL.format(ym=ym)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    with open(tmp_gz, "wb") as fh:
                        async for chunk in resp.content.iter_chunked(1 << 16):
                            fh.write(chunk)
            await asyncio.to_thread(_gunzip, tmp_gz, tmp_out)
            os.replace(tmp_out, path)
            logger.info("GeoIP database updated (%s)", ym)
            return
        except Exception as exc:
            logger.warning("GeoIP download failed for %s: %s", ym, exc)
        finally:
            for f in (tmp_gz, tmp_out):
                try:
                    os.remove(f)
                except OSError:
                    pass
    if not os.path.exists(path):
        logger.warning("GeoIP database unavailable — device locations will be skipped")


def _gunzip(src: str, dst: str) -> None:
    with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            block = fin.read(1 << 20)
            if not block:
                break
            fout.write(block)


def _get_reader():
    """Return a cached geoip2 reader, reopening it if the .mmdb changed on disk."""
    global _reader, _reader_mtime
    if not _HAVE_GEOIP:
        return None
    path = settings.geoip_db_path
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _reader is None or mtime != _reader_mtime:
        try:
            if _reader is not None:
                _reader.close()
            _reader = geoip2.database.Reader(path)
            _reader_mtime = mtime
        except Exception as exc:
            logger.warning("GeoIP reader open failed: %s", exc)
            _reader = None
    return _reader


def _localized(names: dict | None, fallback: str | None) -> str | None:
    if not names:
        return fallback
    return names.get("ru") or names.get("en") or fallback


def flag_emoji(country_code: str | None) -> str:
    """Regional-indicator flag for a 2-letter ISO country code, or '' if invalid."""
    if not country_code or len(country_code) != 2 or not country_code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country_code.upper())


def lookup(ip: str | None) -> tuple[str | None, str | None]:
    """Resolve ``ip`` to ``(location_text, country_code)``.

    ``location_text`` is a localized "City, Country" (or just the country when no
    city is known). Returns ``(None, None)`` when GeoIP is unavailable, the IP is
    missing/private, or the address isn't in the database.
    """
    if not ip:
        return None, None
    reader = _get_reader()
    if reader is None:
        return None, None
    try:
        resp = reader.city(ip)
    except (geoip2.errors.AddressNotFoundError, ValueError):
        return None, None
    except Exception as exc:
        logger.warning("GeoIP lookup failed for %s: %s", ip, exc)
        return None, None

    country_code = resp.country.iso_code
    country = _localized(resp.country.names, resp.country.name)
    city = _localized(resp.city.names, resp.city.name)
    parts = [p for p in (city, country) if p]
    location = ", ".join(parts) if parts else None
    return location, country_code
