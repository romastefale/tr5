from __future__ import annotations

import asyncio
import logging
import weakref

from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models.canvas_processed_file import CanvasProcessedFile
from app.utils.datetime import utcnow_naive as _utcnow_naive

logger = logging.getLogger(__name__)


class CanvasProcessedCacheService:
    """Cache por file_id para mídias derivadas do Canvas.

    Separado de canvas_files para preservar o Canvas bruto como Plano B.
    """

    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def lock(self, cache_key: str) -> asyncio.Lock:
        lock = self._locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[cache_key] = lock
        return lock

    async def get_file_id(self, cache_key: str) -> str | None:
        key = (cache_key or "").strip()
        if not key:
            return None
        try:
            with SessionLocal() as db:
                row = db.get(CanvasProcessedFile, key)
                return row.file_id if row else None
        except Exception:
            logger.warning("canvas_processed_cache get_file_id failed cache_key=%s", key, exc_info=True)
            return None

    async def put(
        self,
        *,
        cache_key: str,
        spotify_track_id: str,
        canvas_fingerprint: str,
        duration_ms: int,
        process_kind: str,
        process_version: str,
        file_id: str,
        file_unique_id: str | None,
    ) -> bool:
        key = (cache_key or "").strip()
        if not key or not spotify_track_id or not file_id:
            return False
        try:
            with SessionLocal() as db:
                try:
                    existing = db.get(CanvasProcessedFile, key)
                    now = _utcnow_naive()
                    if existing:
                        existing.spotify_track_id = spotify_track_id
                        existing.canvas_fingerprint = canvas_fingerprint
                        existing.duration_ms = int(duration_ms)
                        existing.process_kind = process_kind
                        existing.process_version = process_version
                        existing.file_id = file_id
                        existing.file_unique_id = file_unique_id
                        existing.updated_at = now
                    else:
                        db.add(
                            CanvasProcessedFile(
                                cache_key=key,
                                spotify_track_id=spotify_track_id,
                                canvas_fingerprint=canvas_fingerprint,
                                duration_ms=int(duration_ms),
                                process_kind=process_kind,
                                process_version=process_version,
                                file_id=file_id,
                                file_unique_id=file_unique_id,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    db.commit()
                    return True
                except IntegrityError:
                    db.rollback()
                    existing = db.get(CanvasProcessedFile, key)
                    if not existing:
                        return False
                    existing.file_id = file_id
                    existing.file_unique_id = file_unique_id
                    existing.updated_at = _utcnow_naive()
                    db.commit()
                    return True
        except Exception:
            logger.warning("canvas_processed_cache put failed cache_key=%s", key, exc_info=True)
            return False

    async def forget(self, cache_key: str) -> None:
        key = (cache_key or "").strip()
        if not key:
            return
        try:
            with SessionLocal() as db:
                row = db.get(CanvasProcessedFile, key)
                if row:
                    db.delete(row)
                    db.commit()
                    logger.info("canvas_processed_cache forgot stale file_id cache_key=%s", key)
        except Exception:
            logger.warning("canvas_processed_cache forget failed cache_key=%s", key, exc_info=True)


canvas_processed_cache_service = CanvasProcessedCacheService()
