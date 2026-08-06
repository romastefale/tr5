from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref

import httpx

from app.config.settings import SPOTIFY_CANVAS_ENABLED, SPOTIFY_CANVAS_TIMEOUT_SECONDS
from app.security.media import download_media, validate_media_url

logger = logging.getLogger(__name__)
CANVAS_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_CANVAS_URL_RE = re.compile(rb"https://canvaz\.scdn\.co/[^\x00\s\"'<>]+")
_LOOKUP_URL = "https://www.canvasdownloader.com/canvas"
_LOOKUP_MAX_BYTES = 512 * 1024
_POSITIVE_TTL = 24 * 3600
_NEGATIVE_TTL = 30 * 60


def _looks_like_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


class SpotifyCanvasService:
    """Resolve public Canvas assets without user authentication or tokens."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._slots = asyncio.Semaphore(3)

    def _lock(self, track_id: str) -> asyncio.Lock:
        lock = self._locks.get(track_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[track_id] = lock
        return lock

    async def get_canvas_url(self, track_id: str) -> str | None:
        track_id = str(track_id or "").strip()
        if not SPOTIFY_CANVAS_ENABLED or not _TRACK_ID_RE.fullmatch(track_id):
            return None
        cached = self._cache.get(track_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        async with self._lock(track_id):
            cached = self._cache.get(track_id)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            result = await self._lookup(track_id)
            ttl = _POSITIVE_TTL if result else _NEGATIVE_TTL
            self._cache[track_id] = (result, time.monotonic() + ttl)
            if len(self._cache) > 4096:
                self._cache.clear()
            return result

    async def _lookup(self, track_id: str) -> str | None:
        headers = {
            "User-Agent": "myJAMrobot/11.0",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            async with self._slots:
                async with httpx.AsyncClient(timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=False) as client:
                    async with client.stream(
                        "GET",
                        _LOOKUP_URL,
                        params={"link": f"https://open.spotify.com/track/{track_id}"},
                        headers=headers,
                    ) as response:
                        if response.status_code != 200:
                            return None
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > _LOOKUP_MAX_BYTES:
                                return None
                            chunks.append(chunk)
            match = _CANVAS_URL_RE.search(b"".join(chunks))
            if not match:
                return None
            return validate_media_url(match.group(0).decode("ascii", "strict"), kind="canvas")
        except Exception:
            logger.info("CANVAS_LOOKUP_FAILED track_id=%s", track_id)
            return None

    async def download_canvas_bytes(self, url: str) -> bytes | None:
        media = await download_media(
            url,
            kind="canvas",
            max_bytes=CANVAS_DOWNLOAD_MAX_BYTES,
            accepted_types=("video/mp4", "application/octet-stream"),
            timeout_seconds=10.0,
        )
        if not media or not _looks_like_mp4(media.data):
            return None
        return media.data

    async def shutdown(self) -> None:
        self._cache.clear()


spotify_canvas_service = SpotifyCanvasService()
