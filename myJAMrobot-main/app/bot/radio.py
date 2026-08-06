from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.presentation import send_music_card
from app.security.media import sanitize_search_term
from app.security.rate_limit import check_command_rate_limit, enforce_message_rate_limit
from app.services.spotify import spotify_service
from app.services.track_search import TrackHit, search_tracks

router = Router(name="radio")
_PENDING_TTL = 300.0
_PROMPT_TTL = 180.0
_BOUND = 500


@dataclass(slots=True)
class PendingResults:
    hits: list[TrackHit]
    user_id: int
    user_name: str
    chat_id: int
    created_at: float


_pending: dict[str, PendingResults] = {}
_prompts: dict[tuple[int, int], float] = {}


def _purge() -> None:
    now = time.monotonic()
    for token in [token for token, value in _pending.items() if now - value.created_at > _PENDING_TTL]:
        _pending.pop(token, None)
    for key in [key for key, created in _prompts.items() if now - created > _PROMPT_TTL]:
        _prompts.pop(key, None)


def _prompt_answer(message: Message) -> bool:
    if not message.from_user:
        return False
    _purge()
    return (int(message.chat.id), int(message.from_user.id)) in _prompts


def _remember_prompt(chat_id: int, user_id: int) -> None:
    _purge()
    key = (int(chat_id), int(user_id))
    if key not in _prompts and len(_prompts) >= _BOUND:
        oldest = min(_prompts, key=_prompts.get)
        _prompts.pop(oldest, None)
    _prompts[key] = time.monotonic()


async def _show_results(message: Message, *, term: str, user_id: int, user_name: str) -> None:
    term = sanitize_search_term(term)
    if not term:
        await message.answer("Informe o nome da música ou do artista.")
        return
    hits = await search_tracks(term, limit=8)
    if not hits:
        await message.answer(f'Nenhum resultado para “{html.escape(term)}”.')
        return
    _purge()
    if len(_pending) >= _BOUND:
        oldest = min(_pending, key=lambda key: _pending[key].created_at)
        _pending.pop(oldest, None)
    token = secrets.token_urlsafe(7)
    _pending[token] = PendingResults(hits, user_id, user_name, int(message.chat.id), time.monotonic())
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{hit.title} — {hit.artist}"[:64], callback_data=f"radio:{token}:{index}")]
            for index, hit in enumerate(hits)
        ]
    )
    await message.answer("Escolha a faixa:", reply_markup=keyboard)


@router.message(Command("radio"))
async def radio_command(message: Message, command: CommandObject) -> None:
    if not message.from_user or not await enforce_message_rate_limit(message, "radio"):
        return
    term = sanitize_search_term(command.args or "")
    if not term:
        _remember_prompt(int(message.chat.id), int(message.from_user.id))
        await message.answer("Qual música você quer buscar? Responda com o nome ou artista.")
        return
    await _show_results(
        message,
        term=term,
        user_id=int(message.from_user.id),
        user_name=message.from_user.full_name or "Usuário",
    )


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"), _prompt_answer)
async def radio_prompt_answer(message: Message) -> None:
    if not message.from_user:
        return
    key = (int(message.chat.id), int(message.from_user.id))
    if _prompts.pop(key, None) is None:
        return
    if not await enforce_message_rate_limit(message, "radio"):
        return
    await _show_results(
        message,
        term=message.text or "",
        user_id=int(message.from_user.id),
        user_name=message.from_user.full_name or "Usuário",
    )


@router.callback_query(F.data.startswith("radio:"))
async def radio_pick(query: CallbackQuery) -> None:
    if not query.data or not query.message or not query.from_user:
        await query.answer()
        return
    rate = check_command_rate_limit("radio", int(query.from_user.id), int(query.message.chat.id))
    if not rate.allowed:
        await query.answer(f"Aguarde {rate.retry_after_seconds}s.", show_alert=True)
        return
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer()
        return
    pending = _pending.get(parts[1])
    if not pending or time.monotonic() - pending.created_at > _PENDING_TTL:
        _pending.pop(parts[1], None)
        await query.answer("A busca expirou. Use /radio novamente.", show_alert=True)
        return
    if pending.user_id != query.from_user.id:
        await query.answer("Esta busca pertence a outra pessoa.", show_alert=True)
        return
    try:
        hit = pending.hits[int(parts[2])]
    except (ValueError, IndexError):
        await query.answer()
        return
    if _pending.pop(parts[1], None) is None:
        await query.answer()
        return
    await query.answer()

    spotify = await spotify_service.search_track(hit.artist, hit.title)
    cover = hit.cover_large
    if isinstance(spotify, dict) and spotify.get("cover"):
        spotify_area = int(spotify.get("cover_width") or 0) * int(spotify.get("cover_height") or 0)
        if spotify_area > 1_000_000 or not cover:
            cover = spotify.get("cover")
    track = {
        "track_id": f"deezer:{hit.track_id}",
        "track_name": hit.title,
        "artist": hit.artist,
        "spotify_url": (spotify or {}).get("url") if isinstance(spotify, dict) else hit.url,
        "album_image_url": cover,
    }
    await send_music_card(
        query.bot,
        chat_id=pending.chat_id,
        track=track,
        user_id=pending.user_id,
        user_name=pending.user_name,
        footer="Rádio independente · sem conta vinculada",
    )
