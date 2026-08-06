from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.bot.presentation import music_caption_html, send_music_card
from app.bot.telegram import _current_track_or_hint
from app.security.media import download_media, validate_media_url
from app.security.rate_limit import enforce_message_rate_limit
from app.security.work_limits import heavy_job_slot
from app.services.bot_identity import get_bot_identity
from app.services.canvas_asset import get_canvas_bytes_cached
from app.services.canvas_audio import get_canvas_with_preview_asset
from app.services.story_card import render_story_full, render_story_overlay
from app.services.story_video import compose_story_video

logger = logging.getLogger(__name__)
router = Router(name="story")
_MAX_STORY_BYTES = 45 * 1024 * 1024


@router.message(Command("story"))
async def story_command(message: Message) -> None:
    if not message.from_user or not await enforce_message_rate_limit(message, "story"):
        return
    async with heavy_job_slot() as acquired:
        if not acquired:
            await message.answer("O processamento de mídia está ocupado. Tente novamente em instantes.")
            return
        track = await _current_track_or_hint(message)
        if not track:
            return
        try:
            await message.bot.send_chat_action(message.chat.id, "upload_video")
        except Exception:
            pass

        title = str(track.get("track_name") or "").strip()
        artist = str(track.get("artist") or "").strip()
        track_id = str(track.get("spotify_track_id") or track.get("track_id") or "").strip()
        cover_url = validate_media_url(track.get("album_image_url"), kind="cover")
        downloaded = await download_media(
            cover_url,
            kind="cover",
            max_bytes=10 * 1024 * 1024,
            accepted_types=("image/jpeg", "image/png", "image/webp"),
            timeout_seconds=8.0,
        ) if cover_url else None
        cover_bytes = downloaded.data if downloaded else None
        identity = await get_bot_identity(message.bot)
        user_name = message.from_user.full_name or "Usuário"
        listening = f"{user_name} está ouvindo agora"
        caption = music_caption_html(track, user_id=message.from_user.id, user_name=user_name)

        canvas_bytes: bytes | None = None
        processed = await get_canvas_with_preview_asset(
            message.bot,
            track=track,
            track_id=track_id,
            log_prefix="STORY_AUDIO",
            want_bytes=True,
        )
        if processed and processed.bytes_data:
            canvas_id, canvas_bytes = processed.canvas_track_id, processed.bytes_data
        else:
            canvas_id, canvas_bytes = await get_canvas_bytes_cached(
                message.bot,
                track=track,
                track_id=track_id,
                log_prefix="STORY",
            )

        if canvas_bytes:
            overlay = await render_story_overlay(
                cover_bytes=cover_bytes,
                listening=listening,
                title=title,
                artist=artist,
                bot_name=identity.name,
                bot_logo_bytes=identity.photo_bytes,
            )
            video = await compose_story_video(canvas_bytes, overlay) if overlay else None
            if video and len(video) <= _MAX_STORY_BYTES:
                try:
                    await message.answer_video(
                        video=BufferedInputFile(video, filename=f"story-{canvas_id}.mp4"),
                        caption=caption,
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    logger.info("STORY_VIDEO_SEND_FAILED")

        card = await render_story_full(
            cover_bytes=cover_bytes,
            listening=listening,
            title=title,
            artist=artist,
            bot_name=identity.name,
            bot_logo_bytes=identity.photo_bytes,
        )
        if card:
            try:
                await message.answer_photo(
                    photo=BufferedInputFile(card, filename="story-1080x1920.jpg"),
                    caption=caption,
                    parse_mode="HTML",
                )
                return
            except Exception:
                logger.info("STORY_STATIC_SEND_FAILED")

        await send_music_card(
            message.bot,
            chat_id=message.chat.id,
            track=track,
            user_id=message.from_user.id,
            user_name=user_name,
        )
