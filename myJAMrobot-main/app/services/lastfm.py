from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.config.settings import HTTP_TIMEOUT_SECONDS, LASTFM_API_BASE_URL, LASTFM_API_KEY
from app.db.database import SessionLocal
from app.models.lastfm_profile import LastfmProfile
from app.security.media import validate_media_url

logger = logging.getLogger(__name__)
_USERNAME_PATTERN = r"[A-Za-z][A-Za-z0-9_-]{1,14}"
_USERNAME_RE = re.compile(rf"^{_USERNAME_PATTERN}$")
_PREFIX_RE = re.compile(rf"^last\.fm/({_USERNAME_PATTERN})$")
_DEEZER_SEARCH_URL = "https://api.deezer.com/search"
_USERNAME_CACHE_MAX = 4096
_MAX_JSON_BYTES = 1024 * 1024


def normalize_login_username(value: str) -> str:
    """Accept exactly username, @username or last.fm/username."""
    raw = str(value or "").strip()
    match = _PREFIX_RE.fullmatch(raw)
    if match:
        username = match.group(1)
    elif raw.startswith("@") and _USERNAME_RE.fullmatch(raw[1:]):
        username = raw[1:]
    elif _USERNAME_RE.fullmatch(raw):
        username = raw
    else:
        raise ValueError("formato de usuário inválido")
    return username


_clean_username = normalize_login_username


def _stable_track_id(artist: str, title: str) -> str:
    canonical = re.sub(r"\s+", " ", f"{artist}:{title}".casefold()).strip()
    return "lfm:" + hashlib.sha1(canonical.encode(), usedforsecurity=False).hexdigest()[:20]


def _plain(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("#text") or value.get("name") or "").strip()
    return str(value or "").strip()


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold())
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"\([^)]*\)|\[[^]]*]", " ", value)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def _similar(expected: str, found: str) -> bool:
    left, right = _norm(expected), _norm(found)
    return bool(left and right and (left == right or left in right or right in left))


class LastfmService:
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._username_cache: dict[int, str | None] = {}
        self._slots = asyncio.Semaphore(8)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    async def shutdown(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _json_get(self, url: str, *, params: dict[str, str]) -> tuple[int, Any]:
        try:
            async with self._slots:
                async with self._client().stream("GET", url, params=params) as response:
                    status = response.status_code
                    if status != 200:
                        return status, None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_JSON_BYTES:
                            return 413, None
                        chunks.append(chunk)
            return 200, json.loads(b"".join(chunks))
        except Exception:
            return 0, None

    def _cache_username(self, user_id: int, value: str | None) -> None:
        self._username_cache[int(user_id)] = value
        if len(self._username_cache) > _USERNAME_CACHE_MAX:
            self._username_cache.clear()

    async def set_username(self, user_id: int, username: str) -> tuple[str, str | None]:
        clean = normalize_login_username(username)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        previous: str | None = None
        with SessionLocal() as db:
            profile = db.query(LastfmProfile).filter_by(user_id=int(user_id)).first()
            if profile:
                previous = profile.username
                profile.username = clean
                profile.updated_at = now
            else:
                db.add(LastfmProfile(user_id=int(user_id), username=clean, created_at=now, updated_at=now))
            db.commit()
        self._cache_username(user_id, clean)
        logger.info("LASTFM_USERNAME_STORED user_id=%s replaced=%s", int(user_id), bool(previous))
        return clean, previous

    async def get_username(self, user_id: int) -> str | None:
        user_id = int(user_id)
        if user_id in self._username_cache:
            return self._username_cache[user_id]
        with SessionLocal() as db:
            profile = db.query(LastfmProfile).filter_by(user_id=user_id).first()
            username = str(profile.username) if profile else None
        self._cache_username(user_id, username)
        return username

    async def _deezer_cover(self, artist: str, title: str) -> tuple[str | None, int, int]:
        try:
            status, payload = await self._json_get(
                _DEEZER_SEARCH_URL,
                params={"q": f'artist:"{artist}" track:"{title}"', "limit": "8"},
            )
            if status != 200 or not isinstance(payload, dict):
                return None, 0, 0
            items = payload.get("data") or []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                found_artist = str((item.get("artist") or {}).get("name") or "")
                if not _similar(title, str(item.get("title") or "")) or not _similar(artist, found_artist):
                    continue
                album = item.get("album") if isinstance(item.get("album"), dict) else {}
                if album.get("cover_xl"):
                    return str(album["cover_xl"]), 1000, 1000
                if album.get("cover_big"):
                    return str(album["cover_big"]), 500, 500
        except Exception:
            logger.info("DEEZER_COVER_LOOKUP_FAILED")
        return None, 0, 0

    async def get_current_or_last_played(self, user_id: int) -> dict[str, Any] | None:
        username = await self.get_username(user_id)
        if not username or not LASTFM_API_KEY:
            return None
        params = {
            "method": "user.getrecenttracks",
            "user": username,
            "api_key": LASTFM_API_KEY,
            "format": "json",
            "limit": "1",
            "extended": "1",
        }
        try:
            status, payload = await self._json_get(LASTFM_API_BASE_URL, params=params)
            if status != 200:
                logger.info("LASTFM_RECENT_FAILED status=%s user_id=%s", status, int(user_id))
                return None
        except Exception:
            logger.warning("LASTFM_RECENT_FAILED user_id=%s", int(user_id), exc_info=True)
            return None
        tracks = (payload.get("recenttracks") or {}).get("track") or [] if isinstance(payload, dict) else []
        if isinstance(tracks, dict):
            tracks = [tracks]
        if not tracks or not isinstance(tracks[0], dict):
            return None
        return await self._map_track(username, tracks[0])

    async def _map_track(self, username: str, item: dict[str, Any]) -> dict[str, Any] | None:
        title, artist, album = _plain(item.get("name")), _plain(item.get("artist")), _plain(item.get("album"))
        if not title or not artist:
            return None
        images = item.get("image") if isinstance(item.get("image"), list) else []
        lastfm_cover = next((str(img.get("#text")) for img in reversed(images) if isinstance(img, dict) and img.get("#text")), None)
        attr = item.get("@attr") if isinstance(item.get("@attr"), dict) else {}
        now_playing = str(attr.get("nowplaying") or "").casefold() == "true"
        date = item.get("date") if isinstance(item.get("date"), dict) else {}

        from app.services.spotify import spotify_service

        spotify_result, deezer_result = await asyncio.gather(
            spotify_service.search_track(artist, title),
            self._deezer_cover(artist, title),
            return_exceptions=True,
        )
        candidates: list[tuple[str, int, int]] = []
        spotify_url: str | None = None
        preview_url: str | None = None
        spotify_id: str | None = None
        if isinstance(spotify_result, dict):
            spotify_url = str(spotify_result.get("url") or "").strip() or None
            preview_url = str(spotify_result.get("preview_url") or "").strip() or None
            spotify_id = str(spotify_result.get("id") or "").strip() or None
            cover = validate_media_url(str(spotify_result.get("cover") or "").strip(), kind="cover")
            if cover:
                candidates.append((cover, int(spotify_result.get("cover_width") or 0), int(spotify_result.get("cover_height") or 0)))
        if isinstance(deezer_result, tuple):
            deezer_cover = validate_media_url(deezer_result[0], kind="cover")
            if deezer_cover:
                candidates.append((deezer_cover, deezer_result[1], deezer_result[2]))
        lastfm_cover = validate_media_url(lastfm_cover, kind="cover")
        if lastfm_cover:
            candidates.append((lastfm_cover, 300, 300))
        best_cover = max(candidates, key=lambda row: (row[1] * row[2], row[1], row[2]), default=(None, 0, 0))
        item_url = str(item.get("url") or "").strip()
        if not item_url.startswith("https://www.last.fm/"):
            item_url = ""
        track_url = spotify_url or item_url or f"https://www.last.fm/user/{quote(username, safe='')}/library"
        return {
            "source": "lastfm_current" if now_playing else "lastfm_last",
            "played_at": date.get("uts"),
            "track_name": title,
            "artist": artist,
            "album": album,
            "album_name": album,
            "track_id": _stable_track_id(artist, title),
            "spotify_track_id": spotify_id,
            "spotify_url": track_url,
            "album_image_url": best_cover[0],
            "cover_width": best_cover[1],
            "cover_height": best_cover[2],
            "preview_url": preview_url,
        }


lastfm_service = LastfmService()
