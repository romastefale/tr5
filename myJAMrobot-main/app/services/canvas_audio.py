from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram.types import BufferedInputFile

from app.config.settings import (
    CANVAS_AUDIO_PREVIEW_ENABLED,
    CANVAS_AUDIO_PROCESS_VERSION,
    CANVAS_CACHE_CHANNEL_ID,
    CANVAS_CACHE_ENABLED,
    DATA_DIR,
)
from app.security.media import download_media
from app.services.canvas_asset import get_canvas_bytes_cached
from app.services.canvas_cache import is_cacheable_track_id
from app.services.canvas_processed_cache import canvas_processed_cache_service
from app.services.spotify import spotify_service
from app.services.spotify_canvas import CANVAS_DOWNLOAD_MAX_BYTES

logger = logging.getLogger(__name__)

PROCESS_KIND = "canvas_preview_audio"
PROCESS_VERSION = CANVAS_AUDIO_PROCESS_VERSION or "preview-v1"
_PREVIEW_MAX_BYTES = 8 * 1024 * 1024
_OUTPUT_MAX_BYTES = 18 * 1024 * 1024
_MIN_MEDIA_BYTES = 512
_DURATION_TOLERANCE_SECONDS = 0.35
_HTTP_TIMEOUT_SECONDS = 8.0
_FFPROBE_TIMEOUT_SECONDS = 10.0
_FFMPEG_TIMEOUT_SECONDS = 35.0
AUDIO_CANVAS_BYTES_DIR = Path(DATA_DIR) / "canvas_audio_bytes"


@dataclass(slots=True)
class CanvasAudioAsset:
    canvas_track_id: str
    cache_key: str
    file_id: str
    duration_ms: int
    bytes_data: bytes | None = None


def _looks_like_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


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
        return f"{html.escape(name)} — {html.escape(artist)} · preview"
    if name:
        return f"{html.escape(name)} · preview"
    return f"{html.escape(canvas_track_id)} · preview"


def _audio_cache_key(spotify_track_id: str, canvas_fingerprint: str, duration_ms: int) -> str:
    canonical = "|".join(
        (
            PROCESS_KIND,
            PROCESS_VERSION,
            spotify_track_id.strip(),
            canvas_fingerprint.strip(),
            str(int(duration_ms)),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8", "ignore")).hexdigest()


def _safe_audio_path(cache_key: str) -> Path:
    return AUDIO_CANVAS_BYTES_DIR / f"canvas-audio-{cache_key[:24]}.mp4"


def _read_local_audio(cache_key: str, log_prefix: str) -> bytes | None:
    path = _safe_audio_path(cache_key)
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size < _MIN_MEDIA_BYTES or size > _OUTPUT_MAX_BYTES:
            path.unlink(missing_ok=True)
            logger.warning("%s_AUDIO_LOCAL_INVALID cache_key=%s size=%s", log_prefix, cache_key, size)
            return None
        data = path.read_bytes()
        if not _looks_like_mp4(data):
            path.unlink(missing_ok=True)
            logger.warning("%s_AUDIO_LOCAL_INVALID cache_key=%s reason=container", log_prefix, cache_key)
            return None
        logger.info("%s_AUDIO_LOCAL_HIT cache_key=%s bytes=%s", log_prefix, cache_key, len(data))
        return data
    except Exception:
        logger.warning("%s_AUDIO_LOCAL_READ_FAILED cache_key=%s", log_prefix, cache_key, exc_info=True)
        return None


def _write_local_audio(cache_key: str, data: bytes, log_prefix: str) -> None:
    if len(data) < _MIN_MEDIA_BYTES or len(data) > _OUTPUT_MAX_BYTES or not _looks_like_mp4(data):
        logger.warning("%s_AUDIO_LOCAL_WRITE_SKIPPED cache_key=%s bytes=%s", log_prefix, cache_key, len(data))
        return
    try:
        AUDIO_CANVAS_BYTES_DIR.mkdir(parents=True, exist_ok=True)
        path = _safe_audio_path(cache_key)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        logger.info("%s_AUDIO_LOCAL_STORED cache_key=%s bytes=%s", log_prefix, cache_key, len(data))
    except Exception:
        logger.warning("%s_AUDIO_LOCAL_WRITE_FAILED cache_key=%s", log_prefix, cache_key, exc_info=True)


async def _download_telegram_file_id(bot: Any, *, cache_key: str, file_id: str, log_prefix: str) -> bytes | None:
    try:
        tg_file = await bot.get_file(file_id)
        file_path = getattr(tg_file, "file_path", None)
        if not file_path:
            logger.warning("%s_AUDIO_FILEID_NO_PATH cache_key=%s", log_prefix, cache_key)
            return None
        buf = await bot.download_file(file_path)
        data = buf.read() if hasattr(buf, "read") else bytes(buf)
        if len(data) < _MIN_MEDIA_BYTES or len(data) > _OUTPUT_MAX_BYTES or not _looks_like_mp4(data):
            logger.warning("%s_AUDIO_FILEID_BYTES_INVALID cache_key=%s bytes=%s", log_prefix, cache_key, len(data))
            return None
        logger.info("%s_AUDIO_FILEID_BYTES_HIT cache_key=%s bytes=%s", log_prefix, cache_key, len(data))
        return data
    except Exception:
        logger.warning("%s_AUDIO_FILEID_DOWNLOAD_FAILED cache_key=%s", log_prefix, cache_key, exc_info=True)
        return None


async def _ffprobe_duration_seconds(path: str) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        logger.debug("ffprobe duration failed rc=%s err=%s", proc.returncode, (stderr or b"")[-300:])
        return None
    try:
        value = json.loads((stdout or b"{}").decode("utf-8", "replace"))["format"]["duration"]
        duration = float(value)
        return duration if duration > 0 else None
    except Exception:
        logger.debug("ffprobe duration parse failed stdout=%r", stdout, exc_info=True)
        return None


async def _download_preview(preview_url: str, log_prefix: str) -> bytes | None:
    media = await download_media(
        preview_url,
        kind="preview",
        max_bytes=_PREVIEW_MAX_BYTES,
        accepted_types=("audio/mpeg", "audio/mp3", "application/octet-stream"),
        timeout_seconds=_HTTP_TIMEOUT_SECONDS,
    )
    if media and len(media.data) >= _MIN_MEDIA_BYTES:
        return media.data
    logger.info("%s_PREVIEW_DOWNLOAD_SKIPPED", log_prefix)
    return None


async def _preview_url_for_track(track: dict[str, Any], canvas_track_id: str, log_prefix: str) -> str | None:
    existing = str(track.get("preview_url") or "").strip()
    if existing:
        return existing
    try:
        lookup = await spotify_service.get_track_by_id(canvas_track_id, market="BR")
        preview_url = str((lookup or {}).get("preview_url") or "").strip()
        if preview_url:
            return preview_url
    except Exception:
        logger.warning("%s_PREVIEW_LOOKUP_FAILED track_id=%s", log_prefix, canvas_track_id, exc_info=True)
    logger.info("%s_PREVIEW_MISSING track_id=%s", log_prefix, canvas_track_id)
    return None


async def _mux_canvas_with_preview(canvas_bytes: bytes, preview_bytes: bytes, log_prefix: str) -> tuple[bytes, int, str] | None:
    tmp_dir = tempfile.mkdtemp(prefix="canvas_audio_")
    canvas_path = os.path.join(tmp_dir, "canvas.mp4")
    preview_path = os.path.join(tmp_dir, "preview.mp3")
    out_path = os.path.join(tmp_dir, "out.mp4")
    try:
        with open(canvas_path, "wb") as fh:
            fh.write(canvas_bytes)
        with open(preview_path, "wb") as fh:
            fh.write(preview_bytes)

        video_duration = await _ffprobe_duration_seconds(canvas_path)
        audio_duration = await _ffprobe_duration_seconds(preview_path)
        if not video_duration or not audio_duration:
            logger.info("%s_DURATION_PROBE_FAILED video=%s audio=%s", log_prefix, video_duration, audio_duration)
            return None
        if audio_duration + _DURATION_TOLERANCE_SECONDS < video_duration:
            logger.info("%s_DURATION_INCOMPATIBLE video=%.3f audio=%.3f", log_prefix, video_duration, audio_duration)
            return None

        duration_ms = int(round(video_duration * 1000))
        canvas_fingerprint = hashlib.sha256(canvas_bytes).hexdigest()[:32]
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            canvas_path,
            "-i",
            preview_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            f"{video_duration:.3f}",
            "-shortest",
            "-movflags",
            "+faststart",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("%s_FFMPEG_TIMEOUT", log_prefix)
            return None
        if proc.returncode != 0:
            tail = (stderr or b"")[-600:].decode("utf-8", "replace")
            logger.warning("%s_FFMPEG_FAILED rc=%s err=%s", log_prefix, proc.returncode, tail)
            return None
        data = Path(out_path).read_bytes()
        if len(data) < _MIN_MEDIA_BYTES or len(data) > _OUTPUT_MAX_BYTES or not _looks_like_mp4(data):
            logger.warning("%s_OUTPUT_BYTES_INVALID bytes=%s", log_prefix, len(data))
            return None
        return data, duration_ms, canvas_fingerprint
    except Exception:
        logger.warning("%s_MUX_FAILED", log_prefix, exc_info=True)
        return None
    finally:
        for path in (canvas_path, preview_path, out_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


async def _archive_processed_audio(
    bot: Any,
    *,
    track: dict[str, Any],
    canvas_track_id: str,
    cache_key: str,
    data: bytes,
    log_prefix: str,
) -> tuple[str | None, str | None]:
    if not CANVAS_CACHE_CHANNEL_ID:
        logger.info("%s_AUDIO_ARCHIVE_SKIPPED_NO_CHANNEL track_id=%s", log_prefix, canvas_track_id)
        return None, None
    try:
        sent = await bot.send_video(
            chat_id=CANVAS_CACHE_CHANNEL_ID,
            video=BufferedInputFile(data, filename=f"canvas-audio-{canvas_track_id}-{cache_key[:8]}.mp4"),
            caption=_archive_caption(track, canvas_track_id),
            parse_mode="HTML",
        )
        file_id, file_unique_id = _extract_file_ids(sent)
        if not file_id:
            logger.warning("%s_AUDIO_ARCHIVE_NO_FILE_ID track_id=%s", log_prefix, canvas_track_id)
            return None, None
        logger.info("%s_AUDIO_ARCHIVED track_id=%s cache_key=%s", log_prefix, canvas_track_id, cache_key)
        return file_id, file_unique_id
    except Exception:
        logger.warning("%s_AUDIO_ARCHIVE_FAILED track_id=%s", log_prefix, canvas_track_id, exc_info=True)
        return None, None


async def get_canvas_with_preview_asset(
    bot: Any,
    *,
    track: dict[str, Any],
    track_id: str,
    log_prefix: str = "CANVAS_AUDIO",
    want_bytes: bool = False,
) -> CanvasAudioAsset | None:
    """Retorna Canvas muxado com preview oficial, ou None para acionar Plano B.

    O canal de cache é apenas uma otimização. Sem canal (ou se o arquivamento
    falhar), os bytes processados continuam disponíveis para envio direto.
    Qualquer falha de processamento devolve None, sem impedir que /canvas e
    /story usem o Canvas bruto já validado.
    """
    if not CANVAS_AUDIO_PREVIEW_ENABLED:
        return None

    canvas_track_id, canvas_bytes = await get_canvas_bytes_cached(
        bot,
        track=track,
        track_id=track_id,
        log_prefix=f"{log_prefix}_RAW",
    )
    if not canvas_bytes or not is_cacheable_track_id(canvas_track_id):
        return None
    if len(canvas_bytes) > CANVAS_DOWNLOAD_MAX_BYTES:
        logger.info("%s_RAW_CANVAS_TOO_LARGE track_id=%s bytes=%s", log_prefix, canvas_track_id, len(canvas_bytes))
        return None

    canvas_fingerprint = hashlib.sha256(canvas_bytes).hexdigest()[:32]
    tmp_dir = tempfile.mkdtemp(prefix="canvas_audio_probe_")
    probe_path = os.path.join(tmp_dir, "canvas.mp4")
    try:
        Path(probe_path).write_bytes(canvas_bytes)
        video_duration = await _ffprobe_duration_seconds(probe_path)
    finally:
        try:
            os.remove(probe_path)
        except OSError:
            pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
    if not video_duration:
        logger.info("%s_RAW_DURATION_PROBE_FAILED track_id=%s", log_prefix, canvas_track_id)
        return None

    duration_ms = int(round(video_duration * 1000))
    cache_key = _audio_cache_key(canvas_track_id, canvas_fingerprint, duration_ms)

    need_bytes = want_bytes or not CANVAS_CACHE_CHANNEL_ID
    local = _read_local_audio(cache_key, log_prefix) if need_bytes and CANVAS_CACHE_ENABLED else None
    cached_file_id = await canvas_processed_cache_service.get_file_id(cache_key)
    if cached_file_id:
        if not want_bytes:
            logger.info("%s_AUDIO_CACHE_HIT track_id=%s cache_key=%s", log_prefix, canvas_track_id, cache_key)
            return CanvasAudioAsset(canvas_track_id, cache_key, cached_file_id, duration_ms)
        data = local or await _download_telegram_file_id(
            bot, cache_key=cache_key, file_id=cached_file_id, log_prefix=log_prefix
        )
        if data:
            if not local:
                _write_local_audio(cache_key, data, log_prefix)
            logger.info("%s_AUDIO_CACHE_HIT_BYTES track_id=%s cache_key=%s", log_prefix, canvas_track_id, cache_key)
            return CanvasAudioAsset(canvas_track_id, cache_key, cached_file_id, duration_ms, data)
        await canvas_processed_cache_service.forget(cache_key)
    elif local:
        return CanvasAudioAsset(canvas_track_id, cache_key, "", duration_ms, local)

    async with canvas_processed_cache_service.lock(cache_key):
        cached_file_id = await canvas_processed_cache_service.get_file_id(cache_key)
        if cached_file_id:
            if not want_bytes:
                logger.info("%s_AUDIO_CACHE_HIT_LOCKED track_id=%s cache_key=%s", log_prefix, canvas_track_id, cache_key)
                return CanvasAudioAsset(canvas_track_id, cache_key, cached_file_id, duration_ms)
            data = _read_local_audio(cache_key, log_prefix) or await _download_telegram_file_id(
                bot, cache_key=cache_key, file_id=cached_file_id, log_prefix=log_prefix
            )
            if data:
                _write_local_audio(cache_key, data, log_prefix)
                return CanvasAudioAsset(canvas_track_id, cache_key, cached_file_id, duration_ms, data)
            await canvas_processed_cache_service.forget(cache_key)

        preview_url = await _preview_url_for_track(track, canvas_track_id, log_prefix)
        if not preview_url:
            return None
        preview_bytes = await _download_preview(preview_url, log_prefix)
        if not preview_bytes:
            return None
        muxed = await _mux_canvas_with_preview(canvas_bytes, preview_bytes, log_prefix)
        if not muxed:
            return None
        audio_canvas_bytes, mux_duration_ms, mux_fingerprint = muxed
        if mux_fingerprint != canvas_fingerprint:
            logger.info("%s_FINGERPRINT_CHANGED track_id=%s", log_prefix, canvas_track_id)
            return None
        if abs(mux_duration_ms - duration_ms) > 750:
            logger.info("%s_DURATION_CHANGED track_id=%s before=%s after=%s", log_prefix, canvas_track_id, duration_ms, mux_duration_ms)
            return None

        file_id, file_unique_id = await _archive_processed_audio(
            bot,
            track=track,
            canvas_track_id=canvas_track_id,
            cache_key=cache_key,
            data=audio_canvas_bytes,
            log_prefix=log_prefix,
        )
        if file_id:
            stored = await canvas_processed_cache_service.put(
                cache_key=cache_key,
                spotify_track_id=canvas_track_id,
                canvas_fingerprint=canvas_fingerprint,
                duration_ms=duration_ms,
                process_kind=PROCESS_KIND,
                process_version=PROCESS_VERSION,
                file_id=file_id,
                file_unique_id=file_unique_id,
            )
            if not stored:
                logger.warning("%s_AUDIO_CACHE_STORE_FAILED track_id=%s cache_key=%s", log_prefix, canvas_track_id, cache_key)
        if CANVAS_CACHE_ENABLED:
            _write_local_audio(cache_key, audio_canvas_bytes, log_prefix)
        return CanvasAudioAsset(
            canvas_track_id,
            cache_key,
            file_id or "",
            duration_ms,
            audio_canvas_bytes if want_bytes or not file_id else None,
        )
