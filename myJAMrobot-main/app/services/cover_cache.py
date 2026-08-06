"""Cache persistente de capas musicais por ``file_id`` do Telegram.

O banco indexa por faixa, URL e hash; o canal técnico serve somente para obter
um identificador reutilizável. Uma falha de cache nunca bloqueia a entrega: o
chamador continua com a URL ou os bytes originais.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import weakref
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy.exc import IntegrityError

from app.config.settings import COVER_CACHE_CHANNEL_ID, COVER_CACHE_ENABLED
from app.db.database import SessionLocal
from app.models.cover_file import CoverFile
from app.security.media import validate_media_url
from app.utils.datetime import utcnow_naive as _utcnow_naive

logger = logging.getLogger(__name__)


def _clean_track_id(track_id: str | None) -> str | None:
    value = str(track_id or "").strip()
    return value or None


def _clean_cover_url(cover_url: str | None) -> str | None:
    value = str(cover_url or "").strip()
    if not value:
        return None
    if "://" in value:
        return validate_media_url(value, kind="cover")
    return value


def _hash_cover_value(cover: str | bytes | None) -> str | None:
    if cover is None:
        return None
    if isinstance(cover, bytes):
        if not cover:
            return None
        return hashlib.sha1(cover, usedforsecurity=False).hexdigest()
    value = _clean_cover_url(cover)
    if not value:
        return None
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def build_cover_cache_key(track_id: str | None = None, cover_url: str | None = None, cover_hash: str | None = None) -> str | None:
    tid = _clean_track_id(track_id) or ""
    curl = _clean_cover_url(cover_url) or ""
    chash = str(cover_hash or "").strip()
    if not (tid or curl or chash):
        return None
    digest = hashlib.sha1(f"{tid}\0{curl}\0{chash}".encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"cov:{digest}"


@dataclass(slots=True)
class CoverCacheHit:
    cache_key: str
    file_id: str
    file_unique_id: str | None = None
    width: int | None = None
    height: int | None = None


class CoverCacheService:
    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def lock(self, cache_key: str) -> asyncio.Lock:
        lock = self._locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[cache_key] = lock
        return lock

    def _key_for(self, *, track_id: str | None = None, cover_url: str | None = None, photo: str | bytes | None = None) -> tuple[str | None, str | None, str | None]:
        clean_url = _clean_cover_url(cover_url if cover_url is not None else (photo if isinstance(photo, str) else None))
        cover_hash = _hash_cover_value(photo if photo is not None else clean_url)
        cache_key = build_cover_cache_key(track_id, clean_url, cover_hash)
        return cache_key, clean_url, cover_hash

    async def get(self, *, track_id: str | None = None, cover_url: str | None = None, photo: str | bytes | None = None) -> CoverCacheHit | None:
        cache_key, _clean_url, _cover_hash = self._key_for(track_id=track_id, cover_url=cover_url, photo=photo)
        if not cache_key:
            return None
        try:
            with SessionLocal() as db:
                row = db.get(CoverFile, cache_key)
                if not row:
                    return None
                return CoverCacheHit(
                    cache_key=cache_key,
                    file_id=row.file_id,
                    file_unique_id=row.file_unique_id,
                    width=row.width,
                    height=row.height,
                )
        except Exception:
            logger.warning("cover_cache get failed key=%s", cache_key, exc_info=True)
            return None

    async def put(
        self,
        *,
        track_id: str | None = None,
        cover_url: str | None = None,
        photo: str | bytes | None = None,
        file_id: str,
        file_unique_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        if not file_id:
            return
        cache_key, clean_url, cover_hash = self._key_for(track_id=track_id, cover_url=cover_url, photo=photo)
        if not cache_key:
            return
        spotify_track_id = _clean_track_id(track_id)
        if spotify_track_id and spotify_track_id.startswith("lfm:"):
            spotify_track_id = None
        now = _utcnow_naive()
        try:
            with SessionLocal() as db:
                try:
                    row = db.get(CoverFile, cache_key)
                    if row:
                        row.spotify_track_id = spotify_track_id
                        row.cover_url = clean_url
                        row.cover_hash = cover_hash
                        row.file_id = file_id
                        row.file_unique_id = file_unique_id
                        row.width = width
                        row.height = height
                        row.updated_at = now
                    else:
                        db.add(
                            CoverFile(
                                cache_key=cache_key,
                                spotify_track_id=spotify_track_id,
                                cover_url=clean_url,
                                cover_hash=cover_hash,
                                file_id=file_id,
                                file_unique_id=file_unique_id,
                                width=width,
                                height=height,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    row = db.get(CoverFile, cache_key)
                    if row:
                        row.file_id = file_id
                        row.file_unique_id = file_unique_id
                        row.width = width
                        row.height = height
                        row.updated_at = now
                        db.commit()
        except Exception:
            logger.warning("cover_cache put failed key=%s", cache_key, exc_info=True)

    async def forget(self, *, track_id: str | None = None, cover_url: str | None = None, photo: str | bytes | None = None) -> None:
        cache_key, _clean_url, _cover_hash = self._key_for(track_id=track_id, cover_url=cover_url, photo=photo)
        if not cache_key:
            return
        try:
            with SessionLocal() as db:
                row = db.get(CoverFile, cache_key)
                if row:
                    db.delete(row)
                    db.commit()
                    logger.info("cover_cache forgot stale file_id key=%s", cache_key)
        except Exception:
            logger.warning("cover_cache forget failed key=%s", cache_key, exc_info=True)

    async def download_file_id_bytes(self, bot: Bot, file_id: str | None, *, timeout: int = 30) -> bytes | None:
        """Download bytes from a cached Telegram file_id for local rendering.

        The Bot API/aiogram path is: get_file(file_id) -> file_path ->
        download_file(file_path). The returned BytesIO is used only as an
        optimization for composed images; failure must not block fallbacks.
        """
        if not file_id:
            return None
        try:
            tg_file = await bot.get_file(file_id)
            file_path = getattr(tg_file, "file_path", None)
            if not file_path:
                return None
            stream = await bot.download_file(file_path, timeout=timeout)
            try:
                stream.seek(0)
            except Exception:
                pass
            data = stream.read()
            return data if isinstance(data, bytes) and data else None
        except Exception:
            logger.debug("cover_cache download file_id failed", exc_info=True)
            return None

    async def resolve_photo_bytes(
        self,
        bot: Bot,
        *,
        track_id: str | None = None,
        cover_url: str | None = None,
        file_id: str | None = None,
    ) -> bytes | None:
        """Return cover bytes using Telegram cache first, then DB lookup.

        Mosaic rendering needs raw bytes, not just a file_id. When a cached
        file_id is available, download it from Telegram; when it fails, callers
        should fall back to the original cover URL.
        """
        hit = await self.get(track_id=track_id, cover_url=cover_url)
        if hit and hit.file_id:
            if file_id and file_id != hit.file_id:
                logger.info("cover_cache file_id mismatch ignored for current cover key")
            data = await self.download_file_id_bytes(bot, hit.file_id)
            if data:
                return data
            await self.forget(track_id=track_id, cover_url=cover_url)
            return None

        # Só confia em file_id direto quando não há identidade de capa para
        # validar. Nos cards sempre há track_id/cover_url; baixar um file_id
        # antigo antes de verificar a chave foi a causa plausível de capa errada.
        if file_id and not (track_id or cover_url):
            data = await self.download_file_id_bytes(bot, file_id)
            if data:
                return data
        return None

    async def resolve_photo(
        self,
        bot: Bot,
        *,
        track_id: str | None = None,
        cover_url: str | None = None,
        photo: str | bytes | None = None,
        filename: str = "cover.jpg",
    ) -> str | bytes | None:
        """Return a Telegram file_id when possible, otherwise the original photo.

        The return value is safe to pass to send_photo/InputMediaPhoto. Cache
        failures are intentionally transparent: the original URL/bytes remains
        available to the caller.
        """
        original: str | bytes | None = photo if photo is not None else _clean_cover_url(cover_url)
        if isinstance(original, str) and "://" in original:
            original = validate_media_url(original, kind="cover")
        if not original:
            return None
        cache_key, clean_url, _cover_hash = self._key_for(track_id=track_id, cover_url=cover_url, photo=original)
        if not cache_key:
            return original
        if not COVER_CACHE_ENABLED or not COVER_CACHE_CHANNEL_ID:
            return original

        hit = await self.get(track_id=track_id, cover_url=clean_url, photo=original)
        if hit and hit.file_id:
            return hit.file_id

        async with self.lock(cache_key):
            hit = await self.get(track_id=track_id, cover_url=clean_url, photo=original)
            if hit and hit.file_id:
                return hit.file_id
            try:
                if isinstance(original, bytes):
                    sent = await bot.send_photo(
                        chat_id=COVER_CACHE_CHANNEL_ID,
                        photo=BufferedInputFile(original, filename=filename),
                        caption="cover cache",
                    )
                else:
                    sent = await bot.send_photo(
                        chat_id=COVER_CACHE_CHANNEL_ID,
                        photo=str(original),
                        caption="cover cache",
                    )
                sizes: Any = getattr(sent, "photo", None)
                if sizes:
                    best = sizes[-1]
                    file_id = getattr(best, "file_id", None)
                    if file_id:
                        await self.put(
                            track_id=track_id,
                            cover_url=clean_url,
                            photo=original,
                            file_id=file_id,
                            file_unique_id=getattr(best, "file_unique_id", None),
                            width=getattr(best, "width", None),
                            height=getattr(best, "height", None),
                        )
                        return file_id
            except Exception:
                logger.warning("cover_cache archive failed key=%s", cache_key, exc_info=True)
        return original


cover_cache_service = CoverCacheService()
