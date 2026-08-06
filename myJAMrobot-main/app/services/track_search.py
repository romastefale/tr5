from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx

from app.security.media import sanitize_search_term, validate_media_url

logger = logging.getLogger(__name__)
_SEARCH_URL = "https://api.deezer.com/search"
_MAX_JSON_BYTES = 512 * 1024
_SEARCH_SLOTS = asyncio.Semaphore(8)


@dataclass(frozen=True, slots=True)
class TrackHit:
    track_id: str
    title: str
    artist: str
    cover_large: str | None
    cover_thumb: str | None
    url: str | None

    @property
    def cover_big(self) -> str | None:
        return self.cover_large


async def search_tracks(term: str, *, limit: int = 8) -> list[TrackHit]:
    query = sanitize_search_term(term)
    limit = min(8, max(1, int(limit)))
    if not query:
        return []
    try:
        async with _SEARCH_SLOTS:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
                async with client.stream("GET", _SEARCH_URL, params={"q": query, "limit": str(limit)}) as response:
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if response.status_code != 200 or content_type != "application/json":
                        return []
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_JSON_BYTES:
                            return []
                        chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
    except Exception:
        logger.info("RADIO_SEARCH_FAILED")
        return []
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    hits: list[TrackHit] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:300]
        artist = str((item.get("artist") or {}).get("name") or "").strip()[:300]
        if not title or not artist:
            continue
        key = f"{title.casefold()}\0{artist.casefold()}"
        if key in seen:
            continue
        seen.add(key)
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        large = validate_media_url(album.get("cover_xl") or album.get("cover_big"), kind="cover")
        thumb = validate_media_url(album.get("cover_medium") or album.get("cover_small"), kind="cover")
        url = str(item.get("link") or "").strip()
        if not url.startswith("https://www.deezer.com/"):
            url = None
        hits.append(
            TrackHit(
                track_id=str(item.get("id") or "").strip()[:32],
                title=title,
                artist=artist,
                cover_large=large,
                cover_thumb=thumb,
                url=url,
            )
        )
        if len(hits) >= limit:
            break
    return hits
