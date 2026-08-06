from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.presentation import music_caption_html, send_music_card, send_rich_text
from app.security.rate_limit import enforce_message_rate_limit
from app.security.work_limits import lyrics_task_pool
from app.services.connection_check import connect_hint_for
from app.services.lastfm import lastfm_service, normalize_login_username
from app.services.lyrics import lyrics_service
from app.services.music import music_service
from app.services.spotify import spotify_service
from app.services.spotify_canvas import spotify_canvas_service

logger = logging.getLogger(__name__)
bot_dispatcher = Dispatcher()
_REGISTERED = False


def _start_text() -> str:
    return (
        "<b>myJAMrobot</b>\n\n"
        "Informe somente seu nome de usuário da <b>LAST FM</b>:\n"
        "<code>/login username</code>\n\n"
        "Use <code>/help</code> para ver os nove comandos."
    )


def _help_text() -> str:
    return (
        "<b>Comandos</b>\n\n"
        "<code>/start</code> — início\n"
        "<code>/help</code> — ajuda\n"
        "<code>/login</code> — salvar usuário da LAST FM\n"
        "<code>/playing</code> — música atual\n"
        "<code>/canvas</code> — Canvas da música atual\n"
        "<code>/story</code> — story vertical 9:16\n"
        "<code>/radio</code> — busca independente\n"
        "<code>/lyrics</code> — música com trecho de refrão\n"
        "<code>/onoff</code> — proprietário"
    )


def _start_rich() -> str:
    return (
        "<h1>myJAMrobot</h1>"
        "<p>Música usando somente seu nome de usuário da <mark>LAST FM</mark>.</p>"
        "<ol><li>Use <code>/login username</code></li><li>Use <code>/playing</code></li></ol>"
        "<footer>Nenhum OAuth ou redirecionamento externo.</footer>"
    )


def _help_rich() -> str:
    return (
        "<h1>Comandos</h1>"
        "<table bordered striped>"
        "<tr><th>Comando</th><th>Função</th></tr>"
        "<tr><td>/start</td><td>Início</td></tr>"
        "<tr><td>/help</td><td>Ajuda</td></tr>"
        "<tr><td>/login</td><td>Usuário da LAST FM</td></tr>"
        "<tr><td>/playing</td><td>Música atual</td></tr>"
        "<tr><td>/canvas</td><td>Canvas</td></tr>"
        "<tr><td>/story</td><td>Story 9:16</td></tr>"
        "<tr><td>/radio</td><td>Busca independente</td></tr>"
        "<tr><td>/lyrics</td><td>Trecho de refrão</td></tr>"
        "<tr><td>/onoff</td><td>Proprietário</td></tr>"
        "</table>"
    )


async def _current_track_or_hint(message: Message) -> dict[str, Any] | None:
    if not message.from_user:
        return None
    if not await lastfm_service.get_username(message.from_user.id):
        await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML")
        return None
    track = await music_service.get_current_or_last_played(message.from_user.id)
    if not track:
        await message.answer("Não encontrei uma reprodução recente na LAST FM.")
        return None
    return track


def build_playing_payload(message: Message, track: dict[str, Any]) -> tuple[str, str] | None:
    if not message.from_user:
        return None
    track_id = str(track.get("track_id") or "").strip()
    if not track_id:
        return None
    caption = music_caption_html(
        track,
        user_id=message.from_user.id,
        user_name=message.from_user.full_name or "Usuário",
    )
    return track_id, caption


async def _send_playing(message: Message, track: dict[str, Any] | None = None) -> None:
    if not message.from_user:
        return
    track = track or await _current_track_or_hint(message)
    if not track:
        return
    await send_music_card(
        message.bot,
        chat_id=message.chat.id,
        track=track,
        user_id=message.from_user.id,
        user_name=message.from_user.full_name or "Usuário",
    )


def _register_handlers(dp: Dispatcher) -> None:
    global _REGISTERED
    if _REGISTERED:
        return

    @dp.message(Command("start"))
    async def start_command(message: Message) -> None:
        if await enforce_message_rate_limit(message, "start"):
            await send_rich_text(message, rich_html=_start_rich(), fallback_html=_start_text())

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        if await enforce_message_rate_limit(message, "help"):
            await send_rich_text(message, rich_html=_help_rich(), fallback_html=_help_text())

    @dp.message(Command("login"))
    async def login_command(message: Message, command: CommandObject) -> None:
        if not message.from_user or not await enforce_message_rate_limit(message, "login"):
            return
        raw = (command.args or "").strip()
        if not raw:
            await send_rich_text(
                message,
                rich_html=(
                    "<h2>Login da LAST FM</h2>"
                    "<p>Formatos aceitos:</p>"
                    "<ul><li><code>username</code></li><li><code>@username</code></li>"
                    "<li><code>last.fm/username</code></li></ul>"
                    "<footer>Sem OAuth, senha ou redirecionamento.</footer>"
                ),
                fallback_html=(
                    "<b>Login da LAST FM</b>\n"
                    "Use <code>/login username</code>, <code>/login @username</code> ou "
                    "<code>/login last.fm/username</code>."
                ),
            )
            return
        try:
            normalized = normalize_login_username(raw)
        except ValueError:
            await message.answer(
                "Formato inválido. Use username, @username ou last.fm/username.",
            )
            return
        saved, previous = await lastfm_service.set_username(message.from_user.id, normalized)
        replacement = "<p>O nome anterior foi substituído.</p>" if previous and previous.casefold() != saved.casefold() else ""
        await send_rich_text(
            message,
            rich_html=(
                "<h2>Usuário da LAST FM salvo</h2>"
                f"<p>Usuário salvo: <code>{html.escape(saved)}</code></p>{replacement}"
                "<footer>Somente o nome de usuário foi armazenado.</footer>"
            ),
            fallback_html=f"<b>Usuário da LAST FM salvo.</b> <code>{html.escape(saved)}</code>.",
        )

    @dp.message(Command("playing"))
    async def playing_command(message: Message) -> None:
        if await enforce_message_rate_limit(message, "playing"):
            await _send_playing(message)

    _REGISTERED = True


async def shutdown_telegram_bot() -> None:
    await lyrics_task_pool.shutdown()
    await lyrics_service.shutdown()
    await lastfm_service.shutdown()
    await spotify_service.shutdown()
    await spotify_canvas_service.shutdown()
