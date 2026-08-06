"""Canvas bruto compartilhado entre /canvas e /story.

/canvas otimiza entrega por file_id. /story precisa dos bytes do Canvas para
compor o vídeo final com overlay. Este módulo reaproveita a mesma tabela
canvas_files e o mesmo canal de cache, baixando o file_id já conhecido pelo
Telegram antes de voltar ao CDN/Spotify Canvas.

Camadas, em ordem:
1. cache local em DATA_DIR/canvas_bytes (mais rápido para /story);
2. canvas_files -> Telegram get_file/download_file (sem Spotify/CDN externo);
3. Spotify Canvas/CDN -> grava cache local -> arquiva no canal -> grava file_id.

Falhas de cache são no-op: retornam None e deixam o comando cair no fallback.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
from pathlib import Path
from typing import Any

from aiogram.types import BufferedInputFile

from app.config.settings import CANVAS_CACHE_CHANNEL_ID, CANVAS_CACHE_ENABLED, DATA_DIR
from app.services.canvas_cache import canvas_cache_service, is_cacheable_track_id
from app.services.spotify import spotify_service
from app.services.spotify_canvas import CANVAS_DOWNLOAD_MAX_BYTES, spotify_canvas_service

logger = logging.getLogger(__name__)

CANVAS_BYTES_DIR = Path(DATA_DIR) / "canvas_bytes"
_CANVAS_MIN_BYTES = 256
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def _looks_like_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _safe_cache_path(track_id: str) -> Path:
    digest = hashlib.sha256(track_id.encode("utf-8", "ignore")).hexdigest()[:24]
    return CANVAS_BYTES_DIR / f"canvas-{digest}.mp4"


def _read_local_canvas(track_id: str, log_prefix: str) -> bytes | None:
    path = _safe_cache_path(track_id)
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size < _CANVAS_MIN_BYTES or size > CANVAS_DOWNLOAD_MAX_BYTES:
            path.unlink(missing_ok=True)
            logger.warning("%s_LOCAL_CANVAS_INVALID track_id=%s size=%s", log_prefix, track_id, size)
            return None
        data = path.read_bytes()
        if not _looks_like_mp4(data):
            path.unlink(missing_ok=True)
            logger.warning("%s_LOCAL_CANVAS_INVALID track_id=%s reason=container", log_prefix, track_id)
            return None
        logger.info("%s_LOCAL_CANVAS_HIT track_id=%s bytes=%s", log_prefix, track_id, len(data))
        return data
    except Exception:
        logger.warning("%s_LOCAL_CANVAS_READ_FAILED track_id=%s", log_prefix, track_id, exc_info=True)
        return None


def _write_local_canvas(track_id: str, data: bytes, log_prefix: str) -> None:
    if len(data) < _CANVAS_MIN_BYTES or len(data) > CANVAS_DOWNLOAD_MAX_BYTES or not _looks_like_mp4(data):
        logger.warning("%s_LOCAL_CANVAS_WRITE_SKIPPED track_id=%s bytes=%s", log_prefix, track_id, len(data))
        return
    try:
        CANVAS_BYTES_DIR.mkdir(parents=True, exist_ok=True)
        path = _safe_cache_path(track_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        logger.info("%s_LOCAL_CANVAS_STORED track_id=%s bytes=%s", log_prefix, track_id, len(data))
    except Exception:
        logger.warning("%s_LOCAL_CANVAS_WRITE_FAILED track_id=%s", log_prefix, track_id, exc_info=True)


def _extract_file_ids(sent: Any) -> tuple[str | None, str | None]:
    for attr in ("video", "animation", "document"):
        media = getattr(sent, attr, None)
        if media is not None:
            return getattr(media, "file_id", None), getattr(media, "file_unique_id", None)
    return None, None


def _archive_caption(track: dict[str, Any], canvas_track_id: str) -> str:
    name = str(track.get("track_name") or "").strip()
    artist = str(track.get("artist") or "").strip()
    if name and artist:
        return f"{html.escape(name)} — {html.escape(artist)}"
    if name:
        return html.escape(name)
    return html.escape(canvas_track_id)


async def resolve_canvas_track_id(track: dict[str, Any], track_id: str, log_prefix: str = "CANVAS_ASSET") -> str:
    """Resolve lfm:<hash> para track_id Spotify base62, sem mudar a chave histórica."""
    tid = (track_id or "").strip()
    preferred = str(track.get("spotify_track_id") or "").strip()
    if _SPOTIFY_ID_RE.fullmatch(preferred):
        return preferred
    if _SPOTIFY_ID_RE.fullmatch(tid):
        return tid
    artist = str(track.get("artist") or "").strip()
    track_name = str(track.get("track_name") or "").strip()
    if not (artist and track_name):
        return tid
    try:
        match = await spotify_service.search_track(artist, track_name)
        if match and match.get("id"):
            resolved = str(match["id"])
            if _SPOTIFY_ID_RE.fullmatch(resolved):
                logger.info("%s_RESOLVED source=%s spotify=%s", log_prefix, tid[:24], resolved)
                return resolved
        logger.info("%s_RESOLVE_MISS source=%s", log_prefix, tid[:24])
    except Exception:
        logger.warning("%s_RESOLVE_ERROR source=%s", log_prefix, tid[:24])
    return ""


async def _download_telegram_file_id(bot: Any, *, track_id: str, file_id: str, log_prefix: str) -> bytes | None:
    try:
        tg_file = await bot.get_file(file_id)
        file_path = getattr(tg_file, "file_path", None)
        if not file_path:
            logger.warning("%s_FILEID_NO_PATH track_id=%s", log_prefix, track_id)
            return None
        buf = await bot.download_file(file_path)
        data = buf.read() if hasattr(buf, "read") else bytes(buf)
        if len(data) < _CANVAS_MIN_BYTES or len(data) > CANVAS_DOWNLOAD_MAX_BYTES or not _looks_like_mp4(data):
            logger.warning("%s_FILEID_BYTES_INVALID track_id=%s bytes=%s", log_prefix, track_id, len(data))
            return None
        logger.info("%s_FILEID_BYTES_HIT track_id=%s bytes=%s", log_prefix, track_id, len(data))
        return data
    except Exception:
        logger.warning("%s_FILEID_DOWNLOAD_FAILED track_id=%s", log_prefix, track_id, exc_info=True)
        return None


async def _download_spotify_canvas(track_id: str, log_prefix: str) -> bytes | None:
    try:
        canvas_url = await spotify_canvas_service.get_canvas_url(track_id)
        if not canvas_url:
            logger.info("%s_NO_CANVAS track_id=%s", log_prefix, track_id)
            return None
        data = await spotify_canvas_service.download_canvas_bytes(canvas_url)
        if not data:
            logger.info("%s_DOWNLOAD_FAILED track_id=%s", log_prefix, track_id)
            return None
        logger.info("%s_SPOTIFY_BYTES_HIT track_id=%s bytes=%s", log_prefix, track_id, len(data))
        return data
    except Exception:
        logger.exception("%s_DOWNLOAD_ERROR track_id=%s", log_prefix, track_id)
        return None


async def _archive_canvas_bytes(bot: Any, *, track: dict[str, Any], track_id: str, data: bytes, log_prefix: str) -> None:
    if not CANVAS_CACHE_CHANNEL_ID:
        return
    try:
        sent = await bot.send_video(
            chat_id=CANVAS_CACHE_CHANNEL_ID,
            video=BufferedInputFile(data, filename=f"canvas-{track_id}.mp4"),
            caption=_archive_caption(track, track_id),
            parse_mode="HTML",
        )
        file_id, file_unique_id = _extract_file_ids(sent)
        if file_id:
            await canvas_cache_service.put(track_id, file_id, file_unique_id)
            logger.info("%s_ARCHIVED track_id=%s", log_prefix, track_id)
    except Exception:
        logger.warning("%s_ARCHIVE_FAILED track_id=%s", log_prefix, track_id, exc_info=True)


async def get_canvas_bytes_cached(
    bot: Any,
    *,
    track: dict[str, Any],
    track_id: str,
    log_prefix: str = "CANVAS_ASSET",
) -> tuple[str, bytes | None]:
    """Retorna (canvas_track_id, bytes) para composição do /story.

    Usa cache local, depois file_id já arquivado no Telegram, depois CDN/Spotify.
    Nunca levanta exceção para o comando; cache é otimização e o caller mantém
    fallback para card estático.
    """
    canvas_track_id = await resolve_canvas_track_id(track, track_id, log_prefix)
    cache_on = CANVAS_CACHE_ENABLED and is_cacheable_track_id(canvas_track_id)

    if cache_on:
        data = _read_local_canvas(canvas_track_id, log_prefix)
        if data:
            return canvas_track_id, data

        cached_file_id = await canvas_cache_service.get_file_id(canvas_track_id)
        if cached_file_id:
            data = await _download_telegram_file_id(
                bot, track_id=canvas_track_id, file_id=cached_file_id, log_prefix=log_prefix
            )
            if data:
                _write_local_canvas(canvas_track_id, data, log_prefix)
                return canvas_track_id, data
            await canvas_cache_service.forget(canvas_track_id)

    if not cache_on:
        return canvas_track_id, await _download_spotify_canvas(canvas_track_id, log_prefix)

    async with canvas_cache_service.lock(canvas_track_id):
        data = _read_local_canvas(canvas_track_id, log_prefix)
        if data:
            return canvas_track_id, data

        cached_file_id = await canvas_cache_service.get_file_id(canvas_track_id)
        if cached_file_id:
            data = await _download_telegram_file_id(
                bot, track_id=canvas_track_id, file_id=cached_file_id, log_prefix=log_prefix
            )
            if data:
                _write_local_canvas(canvas_track_id, data, log_prefix)
                return canvas_track_id, data
            await canvas_cache_service.forget(canvas_track_id)

        data = await _download_spotify_canvas(canvas_track_id, log_prefix)
        if not data:
            return canvas_track_id, None

        _write_local_canvas(canvas_track_id, data, log_prefix)
        await _archive_canvas_bytes(bot, track=track, track_id=canvas_track_id, data=data, log_prefix=log_prefix)
        return canvas_track_id, data
