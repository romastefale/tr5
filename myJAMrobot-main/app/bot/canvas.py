from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.canvas_delivery import deliver_canvas
from app.bot.telegram import _current_track_or_hint, build_playing_payload
from app.security.rate_limit import enforce_message_rate_limit
from app.security.work_limits import heavy_job_slot

router = Router(name="canvas")


@router.message(Command("canvas"))
async def canvas_command(message: Message) -> None:
    if not message.from_user or not await enforce_message_rate_limit(message, "canvas"):
        return
    async with heavy_job_slot() as acquired:
        if not acquired:
            await message.answer("O processamento de mídia está ocupado. Tente novamente em instantes.")
            return
        track = await _current_track_or_hint(message)
        if not track:
            return
        payload = build_playing_payload(message, track)
        if not payload:
            await message.answer("Não consegui identificar a faixa.")
            return
        track_id, caption = payload
        await deliver_canvas(message, track=track, track_id=track_id, caption=caption)
