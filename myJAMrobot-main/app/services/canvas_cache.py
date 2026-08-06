"""Cache persistente do Spotify Canvas por file_id do Telegram.

O Telegram guarda cada arquivo enviado e devolve um `file_id` que pode ser
REENVIADO pra qualquer chat (pelo MESMO bot) sem re-upload — padrão oficial
documentado (Bot FAQ / grammY docs) e usado por inúmeros projetos de "canal
como storage". Aqui guardamos `track_id (Spotify) -> file_id` em DB pra que
/canvas e /story parem de rebaixar do CDN e re-subir o mesmo vídeo a cada uso.

Robustez (file_id PODE mudar com o tempo — não é garantido estável):
- `file_id` é o que reenvia; se o Telegram rejeitar ("wrong file_id"/400), o
  caller chama `forget()` e re-sobe os bytes.
- `file_unique_id` é estável; guardado só pra dedup/diagnóstico.

Concorrência: lock POR TRACK (mesmo padrão de spotify_canvas) pra coalescer o
trabalho de download+upload de 2 users pedindo a MESMA faixa nova — o 2º espera
e pega o file_id que o 1º acabou de gravar. Todos os métodos são defensivos:
nunca levantam (falha de DB vira no-op + log), porque cache é otimização, não
caminho crítico.
"""
from __future__ import annotations

import asyncio
import logging
import re
import weakref

from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models.canvas_file import CanvasFile
from app.utils.datetime import utcnow_naive as _utcnow_naive

logger = logging.getLogger(__name__)
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def is_cacheable_track_id(track_id: str | None) -> bool:
    """Só Spotify track_id base62 é cacheável. NUNCA o "lfm:<hash>" interno
    (chave histórica de likes) nem vazio — esses jamais resolvem Canvas."""
    tid = (track_id or "").strip()
    return bool(_TRACK_ID_RE.fullmatch(tid))


class CanvasCacheService:
    def __init__(self) -> None:
        # Locks POR TRACK. WeakValueDictionary: o lock vive enquanto algum
        # coroutine o segura via `async with` (ref forte na frame); quando
        # ninguém usa, o GC remove. Coalescência estrita, sem leak, sem bound.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def lock(self, track_id: str) -> asyncio.Lock:
        lock = self._locks.get(track_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[track_id] = lock
        return lock

    async def get_file_id(self, track_id: str) -> str | None:
        if not is_cacheable_track_id(track_id):
            return None
        try:
            with SessionLocal() as db:
                row = db.get(CanvasFile, track_id.strip())
                return row.file_id if row else None
        except Exception:
            logger.warning("canvas_cache get_file_id failed track_id=%s", track_id, exc_info=True)
            return None

    async def put(self, track_id: str, file_id: str, file_unique_id: str | None) -> None:
        if not is_cacheable_track_id(track_id) or not file_id:
            return
        tid = track_id.strip()
        try:
            with SessionLocal() as db:
                try:
                    existing = db.get(CanvasFile, tid)
                    now = _utcnow_naive()
                    if existing:
                        existing.file_id = file_id
                        existing.file_unique_id = file_unique_id
                        existing.updated_at = now
                    else:
                        db.add(
                            CanvasFile(
                                track_id=tid,
                                file_id=file_id,
                                file_unique_id=file_unique_id,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    db.commit()
                except IntegrityError:
                    # Race: outra coroutine inseriu a mesma track entre o get e
                    # o commit. Rollback + update do que já está lá.
                    db.rollback()
                    existing = db.get(CanvasFile, tid)
                    if existing:
                        existing.file_id = file_id
                        existing.file_unique_id = file_unique_id
                        existing.updated_at = _utcnow_naive()
                        db.commit()
        except Exception:
            logger.warning("canvas_cache put failed track_id=%s", track_id, exc_info=True)

    async def forget(self, track_id: str) -> None:
        if not is_cacheable_track_id(track_id):
            return
        try:
            with SessionLocal() as db:
                row = db.get(CanvasFile, track_id.strip())
                if row:
                    db.delete(row)
                    db.commit()
                    logger.info("canvas_cache forgot stale file_id track_id=%s", track_id)
        except Exception:
            logger.warning("canvas_cache forget failed track_id=%s", track_id, exc_info=True)


canvas_cache_service = CanvasCacheService()
