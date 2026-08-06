from __future__ import annotations

import html
import logging
import re
from typing import Any

from aiogram.types import BufferedInputFile, Message

from app.bot.presentation import send_music_card
from app.config.settings import CANVAS_CACHE_CHANNEL_ID, CANVAS_CACHE_ENABLED
from app.services.canvas_audio import get_canvas_with_preview_asset
from app.services.canvas_cache import canvas_cache_service, is_cacheable_track_id
from app.services.canvas_processed_cache import canvas_processed_cache_service
from app.services.spotify import spotify_service
from app.services.spotify_canvas import spotify_canvas_service

logger = logging.getLogger(__name__)
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def _file_ids(message: Message) -> tuple[str | None, str | None]:
    for attribute in ("video", "animation", "document"):
        media = getattr(message, attribute, None)
        if media:
            return getattr(media, "file_id", None), getattr(media, "file_unique_id", None)
    return None, None


async def _canvas_track_id(track: dict[str, Any], track_id: str) -> str | None:
    direct = str(track.get("spotify_track_id") or "").strip()
    if _SPOTIFY_ID_RE.fullmatch(direct):
        return direct
    if _SPOTIFY_ID_RE.fullmatch(track_id):
        return track_id
    match = await spotify_service.search_track(
        str(track.get("artist") or ""),
        str(track.get("track_name") or ""),
    )
    candidate = str((match or {}).get("id") or "").strip()
    return candidate if _SPOTIFY_ID_RE.fullmatch(candidate) else None


async def deliver_canvas(
    message: Message,
    *,
    track: dict[str, Any],
    track_id: str,
    caption: str,
) -> Message:
    if not message.from_user:
        raise ValueError("message.from_user is required")
    bot = message.bot

    async def fallback() -> Message:
        delivery = await send_music_card(
            bot,
            chat_id=message.chat.id,
            track=track,
            user_id=message.from_user.id,
            user_name=message.from_user.full_name or "Usuário",
        )
        return delivery.message

    canvas_id = await _canvas_track_id(track, track_id)
    if not canvas_id:
        return await fallback()
    cache_enabled = CANVAS_CACHE_ENABLED and is_cacheable_track_id(canvas_id)

    if cache_enabled:
        processed = await get_canvas_with_preview_asset(
            bot,
            track=track,
            track_id=canvas_id,
            log_prefix="CANVAS_AUDIO",
            want_bytes=False,
        )
        if processed and processed.file_id:
            try:
                return await message.answer_video(video=processed.file_id, caption=caption, parse_mode="HTML")
            except Exception:
                await canvas_processed_cache_service.forget(processed.cache_key)
        if processed and processed.bytes_data:
            try:
                return await message.answer_video(
                    video=BufferedInputFile(
                        processed.bytes_data,
                        filename=f"canvas-audio-{processed.canvas_track_id}.mp4",
                    ),
                    caption=caption,
                    parse_mode="HTML",
                )
            except Exception:
                logger.info("CANVAS_AUDIO_DIRECT_SEND_FAILED track_id=%s", processed.canvas_track_id)

        cached = await canvas_cache_service.get_file_id(canvas_id)
        if cached:
            try:
                return await message.answer_video(video=cached, caption=caption, parse_mode="HTML")
            except Exception:
                await canvas_cache_service.forget(canvas_id)

    url = await spotify_canvas_service.get_canvas_url(canvas_id)
    data = await spotify_canvas_service.download_canvas_bytes(url or "") if url else None
    if not data:
        return await fallback()

    filename = f"canvas-{canvas_id}.mp4"
    if cache_enabled and CANVAS_CACHE_CHANNEL_ID:
        try:
            archived = await bot.send_video(
                chat_id=CANVAS_CACHE_CHANNEL_ID,
                video=BufferedInputFile(data, filename=filename),
                caption=f"{html.escape(str(track.get('track_name') or ''))} — {html.escape(str(track.get('artist') or ''))}",
            )
            file_id, unique_id = _file_ids(archived)
            if file_id:
                await canvas_cache_service.put(canvas_id, file_id, unique_id)
                return await message.answer_video(video=file_id, caption=caption, parse_mode="HTML")
        except Exception:
            logger.info("CANVAS_ARCHIVE_FAILED track_id=%s", canvas_id)

    try:
        sent = await message.answer_video(
            video=BufferedInputFile(data, filename=filename),
            caption=caption,
            parse_mode="HTML",
        )
    except Exception:
        logger.info("CANVAS_SEND_FAILED track_id=%s", canvas_id)
        return await fallback()
    if cache_enabled:
        file_id, unique_id = _file_ids(sent)
        if file_id:
            await canvas_cache_service.put(canvas_id, file_id, unique_id)
    return sent
