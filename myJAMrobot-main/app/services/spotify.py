from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import timedelta
from typing import Any

import httpx

from app.config.settings import HTTP_TIMEOUT_SECONDS, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
from app.security.media import validate_media_url
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_CACHE_MAX = 4096
_MAX_JSON_BYTES = 1024 * 1024


def _largest_image(images: object) -> tuple[str | None, int, int]:
    best: tuple[str | None, int, int] = (None, 0, 0)
    if not isinstance(images, list):
        return best
    for image in images:
        if not isinstance(image, dict):
            continue
        url = str(image.get("url") or "").strip()
        try:
            width = max(0, int(image.get("width") or 0))
            height = max(0, int(image.get("height") or 0))
        except (TypeError, ValueError):
            width = height = 0
        if url and (width * height, width, height) > (best[1] * best[2], best[1], best[2]):
            best = (url, width, height)
    return best


class SpotifyService:
    """App-only Spotify metadata client. No user OAuth or token persistence."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at = utcnow_naive()
        self._token_lock = asyncio.Lock()
        self._request_slots = asyncio.Semaphore(6)
        self._search_cache: dict[tuple[str, str], tuple[dict[str, Any] | None, object]] = {}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    async def shutdown(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._token = None

    async def _access_token(self) -> str | None:
        if self._token and self._token_expires_at > utcnow_naive() + timedelta(seconds=60):
            return self._token
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            return None
        async with self._token_lock:
            if self._token and self._token_expires_at > utcnow_naive() + timedelta(seconds=60):
                return self._token
            basic = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
            try:
                response = await self._client().post(
                    "https://accounts.spotify.com/api/token",
                    data={"grant_type": "client_credentials"},
                    headers={"Authorization": f"Basic {basic}"},
                )
                if response.status_code != 200:
                    logger.warning("SPOTIFY_APP_TOKEN_FAILED status=%s", response.status_code)
                    return None
                payload = response.json()
                token = str(payload.get("access_token") or "")
                lifetime = int(payload.get("expires_in") or 0)
            except Exception:
                logger.warning("SPOTIFY_APP_TOKEN_FAILED", exc_info=True)
                return None
            if not token or lifetime <= 0:
                return None
            self._token = token
            self._token_expires_at = utcnow_naive() + timedelta(seconds=lifetime)
            return token

    @staticmethod
    def _map_track(item: dict[str, Any], *, source: str) -> dict[str, Any] | None:
        if not isinstance(item, dict) or not item:
            return None
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        artists = item.get("artists") if isinstance(item.get("artists"), list) else []
        artist = str((artists[0] if artists and isinstance(artists[0], dict) else {}).get("name") or "").strip()
        title = str(item.get("name") or "").strip()
        if not title or not artist:
            return None
        cover, width, height = _largest_image(album.get("images"))
        cover = validate_media_url(cover, kind="cover")
        if not cover:
            width = height = 0
        preview_url = validate_media_url(str(item.get("preview_url") or "").strip(), kind="preview")
        return {
            "source": source,
            "track_name": title,
            "artist": artist,
            "album": str(album.get("name") or "").strip(),
            "album_name": str(album.get("name") or "").strip(),
            "track_id": str(item.get("id") or "").strip(),
            "spotify_url": str((item.get("external_urls") or {}).get("spotify") or "").strip() or None,
            "album_image_url": cover,
            "cover_width": width,
            "cover_height": height,
            "preview_url": preview_url,
            "duration_ms": item.get("duration_ms"),
        }

    async def _authorized_get(self, url: str, **kwargs: Any) -> httpx.Response | None:
        token = await self._access_token()
        if not token:
            return None

        async def request_once(access_token: str) -> httpx.Response:
            async with self._client().stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                **kwargs,
            ) as response:
                if response.status_code != 200:
                    return httpx.Response(response.status_code)
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_JSON_BYTES:
                        return httpx.Response(413)
                    chunks.append(chunk)
            return httpx.Response(200, content=b"".join(chunks))

        async with self._request_slots:
            try:
                response = await request_once(token)
                if response.status_code == 401:
                    self._token = None
                    token = await self._access_token()
                    if token:
                        response = await request_once(token)
                return response
            except Exception:
                logger.warning("SPOTIFY_METADATA_REQUEST_FAILED", exc_info=True)
                return None

    async def get_track_by_id(self, track_id: str, market: str | None = "BR") -> dict[str, Any] | None:
        clean = str(track_id or "").strip()
        if not _TRACK_ID_RE.fullmatch(clean):
            return None
        response = await self._authorized_get(
            f"https://api.spotify.com/v1/tracks/{clean}",
            params={"market": market} if market else None,
        )
        if response is None or response.status_code != 200:
            return None
        try:
            return self._map_track(response.json(), source="spotify_metadata")
        except Exception:
            return None

    async def search_track(self, artist: str, title: str) -> dict[str, Any] | None:
        artist = str(artist or "").strip()[:200]
        title = str(title or "").strip()[:200]
        if not artist or not title:
            return None
        key = (artist.casefold(), title.casefold())
        now = utcnow_naive()
        cached = self._search_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
        response = await self._authorized_get(
            "https://api.spotify.com/v1/search",
            params={"q": f'track:"{title}" artist:"{artist}"', "type": "track", "limit": 3, "market": "BR"},
        )
        record: dict[str, Any] | None = None
        if response is not None and response.status_code == 200:
            try:
                items = ((response.json().get("tracks") or {}).get("items") or [])
                if items:
                    mapped = self._map_track(items[0], source="spotify_search")
                    if mapped:
                        record = {
                            "id": mapped["track_id"],
                            "url": mapped["spotify_url"],
                            "cover": mapped["album_image_url"],
                            "cover_width": mapped["cover_width"],
                            "cover_height": mapped["cover_height"],
                            "preview_url": mapped["preview_url"],
                        }
            except Exception:
                logger.warning("SPOTIFY_SEARCH_PARSE_FAILED", exc_info=True)
        self._search_cache[key] = (record, now + (timedelta(hours=24) if record else timedelta(minutes=30)))
        if len(self._search_cache) > _CACHE_MAX:
            self._search_cache.clear()
        return record


spotify_service = SpotifyService()
