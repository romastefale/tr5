from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.presentation import CardDelivery, edit_music_card, send_music_card
from app.bot.telegram import _current_track_or_hint
from app.security.rate_limit import enforce_message_rate_limit
from app.security.work_limits import lyrics_task_pool
from app.services.lyrics import lyrics_service

logger = logging.getLogger(__name__)
router = Router(name="lyrics")


async def _finish_lyrics(
    delivery: CardDelivery,
    *,
    track: dict,
    user_id: int,
    user_name: str,
) -> None:
    artist = str(track.get("artist") or "").strip()
    title = str(track.get("track_name") or "").strip()
    excerpt = await lyrics_service.get_excerpt(artist, title)
    try:
        await edit_music_card(
            delivery,
            track=track,
            user_id=user_id,
            user_name=user_name,
            lyric=excerpt,
            lyric_status=None if excerpt else "Trecho não localizado.",
        )
    except Exception:
        logger.info("LYRICS_CARD_EDIT_FAILED chat_id=%s", delivery.message.chat.id)


@router.message(Command("lyrics"))
async def lyrics_command(message: Message) -> None:
    if not message.from_user or not await enforce_message_rate_limit(message, "lyrics"):
        return
    track = await _current_track_or_hint(message)
    if not track:
        return
    user_id = int(message.from_user.id)
    user_name = message.from_user.full_name or "Usuário"
    delivery = await send_music_card(
        message.bot,
        chat_id=message.chat.id,
        track=track,
        user_id=user_id,
        user_name=user_name,
        lyric_status="Buscando um trecho curto de refrão…",
    )
    key = f"lyrics:{message.chat.id}:{user_id}"
    accepted = lyrics_task_pool.submit(
        key,
        lambda: _finish_lyrics(
            delivery,
            track=track,
            user_id=user_id,
            user_name=user_name,
        ),
    )
    if not accepted:
        try:
            await edit_music_card(
                delivery,
                track=track,
                user_id=user_id,
                user_name=user_name,
                lyric=None,
                lyric_status="Busca ocupada. Tente novamente em instantes.",
            )
        except Exception:
            pass
