from __future__ import annotations

import html
import logging
import uuid

from aiogram import Dispatcher, F
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultPhoto,
    Message,
    MessageReactionUpdated,
    ReactionTypeEmoji,
)

from app.bot.intent import detect_intent
from app.config.settings import LASTFM_API_KEY, is_code_owner
from app.services.connection_check import connect_hint_for, is_user_connected
from app.services.lastfm import lastfm_service
from app.services.likes import likes_service
from app.services.music import music_service
from app.services.reactions import reactions_service
from app.services.spotify import spotify_service

logger = logging.getLogger(__name__)



bot_dispatcher: Dispatcher = Dispatcher()


# Sprint 9 (#8): IDs públicos de Message Effects (Premium / Bot API 7.7+).
# Telegram só aplica em chats privados; em grupos é silenciosamente
# ignorado. Wrap em try/except no caller pra cair pra send normal se
# o ID for inválido pra esse user/região (ex: Premium-only effects).
_EFFECT_FIRE = "5104841245755180586"      # 🔥
_EFFECT_PARTY = "5046509860389126442"     # 🎉
_EFFECT_THUMBS_UP = "5107584321108051014"  # 👍

# Sprint 10: emojis pra bot reagir nos próprios cards de música.
# Telegram restringe reactions de bots não-Premium à lista oficial
# (👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 ⚡ 💯 🏆 ❤‍🔥 etc) —
# 🎵 e 🎶 NÃO entram. 🔥 é o melhor proxy "musical/energético"; ❤
# marca milestone a cada 5 plays Last.fm.
_CARD_EMOJI_DEFAULT = "🔥"
_CARD_EMOJI_LOVED = "❤"
_CARD_EMOJI_EXTRACT = "🏆"  # extratos visuais: /myself, /weekfm, /monthfm, /songcharts
_CARD_EMOJI_TNOW = "🔥"      # mosaico /tnow (energia do grupo)
_LOVED_PLAYS_THRESHOLD = 5


def _pick_card_emoji(total_plays: int, plays_source: str) -> str:
    """Decide emoji do bot pro card. Múltiplo de 5 plays Last.fm = ❤; resto = 🔥."""
    if (
        plays_source == "lastfm"
        and total_plays > 0
        and total_plays % _LOVED_PLAYS_THRESHOLD == 0
    ):
        return _CARD_EMOJI_LOVED
    return _CARD_EMOJI_DEFAULT


async def _react_to_own_card(bot, chat_id: int, message_id: int, emoji: str) -> None:
    """Sprint 10: bot reage no card que ele mesmo enviou. Silencioso em falha
    (a plataforma pode rejeitar o emoji em alguns casos)."""
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:
        logger.debug(
            "OWN_CARD_REACT_FAILED chat=%s msg=%s emoji=%s",
            chat_id, message_id, emoji, exc_info=True,
        )


async def _answer_with_effect(message: Message, text: str, effect_id: str, **kwargs) -> Message:
    """Sprint 9 (#8): tenta enviar com message_effect_id; cai pra send normal em falha.

    Effects só funcionam em DM. Em grupo o Telegram costuma ignorar
    silenciosamente, mas algumas versões rejeitam — try/except garante
    que o user sempre recebe a mensagem.
    """
    if message.chat.type == "private":
        try:
            return await message.answer(text, message_effect_id=effect_id, **kwargs)
        except Exception:
            logger.debug("EFFECT_SEND_FAILED effect_id=%s", effect_id, exc_info=True)
    return await message.answer(text, **kwargs)


_SANS_BOLD_ITALIC_UPPER_OFFSET = 0x1D63C - ord("A")
_SANS_BOLD_ITALIC_LOWER_OFFSET = 0x1D656 - ord("a")


def _inline_public_name_style(value: str | None) -> str:
    raw = (value or "Usuário").strip() or "Usuário"
    out: list[str] = []
    for ch in raw:
        if "A" <= ch <= "Z":
            out.append(chr(ord(ch) + _SANS_BOLD_ITALIC_UPPER_OFFSET))
        elif "a" <= ch <= "z":
            out.append(chr(ord(ch) + _SANS_BOLD_ITALIC_LOWER_OFFSET))
        else:
            out.append(ch)
    return html.escape("".join(out))



def _track_label(track: dict) -> tuple[str, str, str, str | None]:
    track_name = html.escape(str(track.get("track_name") or ""))
    artist = html.escape(str(track.get("artist") or ""))
    url = html.escape(str(track.get("spotify_url") or ""), quote=True)
    cover = track.get("album_image_url")
    return track_name, artist, url, str(cover) if cover else None


def _user_mention(message: Message) -> str:
    if not message.from_user:
        return "Usuário"
    display_name = html.escape(message.from_user.full_name or "Usuário")
    return f'<b><a href="tg://user?id={message.from_user.id}">{display_name}</a></b>'


def _is_owner_message(message: Message) -> bool:
    return bool(message.from_user and is_code_owner(message.from_user.id))


def _radiofm_prompt_pending(message: Message) -> bool:
    """Detecta resposta pendente do /radiofm sem consumir a mensagem.

    O dispatcher registra um handler textual genérico no módulo principal. Em
    aiogram 3, handler que retorna None conta como tratado e pode impedir
    sub-routers de receberem a mesma mensagem. Quando existir pergunta
    pendente do /radiofm, este handler precisa devolver UNHANDLED para que o
    router específico do RadioFM processe a resposta.
    """
    try:
        from app.bot.radiofm import _is_radiofm_prompt_answer

        return bool(_is_radiofm_prompt_answer(message))
    except Exception:
        logger.debug("RADIOFM_PROMPT_PENDING_CHECK_FAILED", exc_info=True)
        return False


def _should_handle_text_alias(message: Message) -> bool:
    if _radiofm_prompt_pending(message):
        return False
    return detect_intent(message.text or "") == "play"


def _start_text(message: Message) -> str:
    if message.chat.type != "private":
        return (
            "♫ ♥ <b>tigraoRADIO no grupo</b>\n\n"
            "Comandos musicais ativos para compartilhar o que está tocando, buscar músicas, "
            "ver capas, canvas, letras, mosaicos e rankings do grupo.\n\n"
            "<b>Conectar Last.fm:</b> <code>/lastfm seu_username</code>\n"
            "<b>Mostrar música atual:</b> <code>/playing</code>\n"
            "<b>Buscar música:</b> <code>/radiofm nome da música</code>\n"
            "<b>Mosaico do grupo:</b> <code>/tnow</code>\n"
            "<b>Ranking do grupo:</b> <code>/songcharts</code>\n\n"
            "Use <code>/help</code> para ver os comandos disponíveis aqui."
        )

    if _is_owner_message(message):
        return (
            "♫ ♥ <b>tigraoRADIO</b>\n\n"
            "Sua central musical está ativa. Use esta conversa para conectar serviços, "
            "acompanhar sua música atual, gerar cards, extratos e rankings por DM.\n\n"
            "<b>Conectar Last.fm:</b> <code>/lastfm seu_username</code>\n"
            "<b>Conectar Spotify:</b> <code>/login</code>\n"
            "<b>Música atual:</b> <code>/playing</code>\n"
            "<b>Buscar música:</b> <code>/radiofm nome da música</code>\n"
            "<b>Resumo visual:</b> <code>/tnowall</code>\n\n"
            "Use <code>/help</code> para ver os comandos disponíveis nesta conversa."
        )

    return (
        "♫ ♥ <b>Bem-vindo ao tigraoRADIO</b>\n\n"
        "Conecte seu Last.fm para acompanhar o que você está ouvindo, gerar cards, "
        "ver extratos e usar recursos musicais do bot.\n\n"
        "<b>Conectar Last.fm:</b> <code>/lastfm seu_username</code>\n"
        "<b>Conectar Spotify:</b> <code>/login</code>\n"
        "<b>Música atual:</b> <code>/playing</code>\n"
        "<b>Buscar música:</b> <code>/radiofm nome da música</code>\n\n"
        "Use <code>/help</code> para ver seus comandos disponíveis."
    )


def _help_text(message: Message) -> str:
    if message.chat.type != "private":
        return (
            "<b>Comandos do grupo</b>\n\n"
            "<code>/playing</code> — mostra sua música atual.\n"
            "<code>/albnow</code> — destaca o álbum da música atual.\n"
            "<code>/tcanvas</code> — envia o Canvas Spotify da música atual.\n"
            "<code>/tstory</code> — monta story da música atual.\n"
            "<code>/tly</code> — envia trecho de letra da música atual.\n"
            "<code>/radiofm</code> — busca uma música; aceita o termo junto ou resposta depois.\n"
            "<code>/tnow</code> — monta o mosaico de ouvintes.\n"
            "<code>/myself</code> — abre seus extratos.\n"
            "<code>/weekfm</code> — mostra seu extrato semanal Last.fm.\n"
            "<code>/monthfm</code> — mostra seu extrato mensal Last.fm.\n"
            "<code>/songcharts</code> — mostra o ranking musical do grupo.\n"
            "<code>/lastfm</code> — conecta ou mostra seu Last.fm.\n"
            "<code>/lastfmoff</code> — remove seu Last.fm.\n"
            "<code>/help</code> — mostra esta lista."
        )

    if _is_owner_message(message):
        return (
            "<b>Comandos da sua DM</b>\n\n"
            "<code>/start</code> — abre a apresentação do bot.\n"
            "<code>/help</code> — mostra esta lista.\n"
            "<code>/lastfm</code> — conecta ou mostra seu Last.fm.\n"
            "<code>/lastfmoff</code> — remove seu Last.fm.\n"
            "<code>/login</code> — conecta Spotify.\n"
            "<code>/logout</code> — desconecta Spotify.\n"
            "<code>/playing</code> — mostra sua música atual.\n"
            "<code>/albnow</code> — destaca o álbum da música atual.\n"
            "<code>/tcanvas</code> — envia o Canvas Spotify da música atual.\n"
            "<code>/tstory</code> — monta story da música atual.\n"
            "<code>/tly</code> — envia trecho de letra da música atual.\n"
            "<code>/radiofm</code> — busca uma música; aceita o termo junto ou resposta depois.\n"
            "<code>/nowp</code> — envia sua música atual para um grupo em comum.\n"
            "<code>/myself</code> — abre seus extratos.\n"
            "<code>/weekfm</code> — mostra seu extrato semanal Last.fm.\n"
            "<code>/monthfm</code> — mostra seu extrato mensal Last.fm.\n"
            "<code>/tnowall</code> — monta um mosaico consolidado por DM.\n"
            "<code>/songchartsall</code> — monta ranking consolidado por DM.\n"
            "<code>/weekall</code> — monta ranking semanal consolidado por DM.\n"
            "<code>/monthall</code> — monta ranking mensal consolidado por DM."
        )

    return (
        "<b>Comandos da sua DM</b>\n\n"
        "<code>/start</code> — abre a apresentação do bot.\n"
        "<code>/help</code> — mostra esta lista.\n"
        "<code>/lastfm</code> — conecta ou mostra seu Last.fm.\n"
        "<code>/lastfmoff</code> — remove seu Last.fm.\n"
        "<code>/login</code> — conecta Spotify.\n"
        "<code>/logout</code> — desconecta Spotify.\n"
        "<code>/playing</code> — mostra sua música atual.\n"
        "<code>/albnow</code> — destaca o álbum da música atual.\n"
        "<code>/tcanvas</code> — envia o Canvas Spotify da música atual.\n"
        "<code>/tstory</code> — monta story da música atual.\n"
        "<code>/tly</code> — envia trecho de letra da música atual.\n"
        "<code>/radiofm</code> — busca uma música; aceita o termo junto ou resposta depois.\n"
        "<code>/nowp</code> — envia sua música atual para um grupo em comum.\n"
        "<code>/myself</code> — abre seus extratos.\n"
        "<code>/weekfm</code> — mostra seu extrato semanal Last.fm.\n"
        "<code>/monthfm</code> — mostra seu extrato mensal Last.fm."
    )


# Negrito unicode (Mathematical Bold) pro nome de exibição no /tly. Telegram
# já bolda com <b>, mas o /tly quer o nome em fonte negrito-unicode distinta
# (ex.: 𝐏𝐈) renderizada inline no texto. Mapeia só A-Z/a-z/0-9 — caracteres
# sem equivalente nesse bloco unicode (acentos PT-BR como ã/é, espaço,
# pontuação) ficam na forma original.
_BOLD_UPPER_OFFSET = 0x1D400 - ord("A")
_BOLD_LOWER_OFFSET = 0x1D41A - ord("a")
_BOLD_DIGIT_OFFSET = 0x1D7CE - ord("0")


def _bold_unicode(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(ord(ch) + _BOLD_UPPER_OFFSET))
        elif "a" <= ch <= "z":
            out.append(chr(ord(ch) + _BOLD_LOWER_OFFSET))
        elif "0" <= ch <= "9":
            out.append(chr(ord(ch) + _BOLD_DIGIT_OFFSET))
        else:
            out.append(ch)
    return "".join(out)


async def _resolve_play_button_count(user_id: int, track_id: str, artist: str | None, track_name: str | None) -> tuple[int, str]:
    if artist and track_name:
        lastfm_count = await lastfm_service.get_user_track_playcount(user_id, artist, track_name)
        if lastfm_count is not None:
            return lastfm_count, "lastfm"
    return await likes_service.get_track_play_count(track_id), "local"


async def build_playing_payload_for_user(
    user_id: int, display_name_raw: str, track: dict
) -> tuple[str, str, str | None, None, str] | None:
    """Variante que aceita user_id/display_name explícitos.

    Usada por /nowp (envio remoto via callback, onde `message.from_user` seria
    o bot e não o user real). Mesma lógica/saída de `build_playing_payload`.

    Registra a play ANTES de resolver counts/likes pra que o counter exibido
    no botão reflita o estado atualizado (inclui a play recém-registrada).
    Em caso de falha no envio posterior, ficaria um "phantom play" no DB —
    trade-off aceito porque o oposto (counter N-1 visível) é pior pro user.
    """
    track_id = str(track.get("track_id") or "").strip()
    if not track_id:
        return None

    track_name_raw = str(track.get("track_name") or "").strip()
    artist_raw = str(track.get("artist") or "").strip()
    try:
        await likes_service.register_play(user_id, track_id, track_name=track_name_raw, artist_name=artist_raw)
    except Exception:
        logger.exception("REGISTER_PLAY_FAILED user=%s track=%s", user_id, track_id)

    total_plays, plays_source = await _resolve_play_button_count(user_id, track_id, artist_raw, track_name_raw)
    total_likes = await likes_service.get_total_likes(track_id, owner_user_id=user_id)
    user_total_likes = await likes_service.get_user_received_likes(user_id)
    liked = await likes_service.is_track_liked(user_id, track_id, owner_user_id=user_id)

    display_name = html.escape(display_name_raw or "Usuário")
    user_link = f"tg://user?id={user_id}"
    track_name, artist, track_url, cover = _track_label(track)
    track_part = f'<a href="{track_url}">{track_name}</a>' if track_url else track_name
    # Sprint 8: caption agora mostra "♫ N · track — artist" onde N é o
    # playcount per-user (Last.fm ou local). Botões ♫ plays e ♥ likes
    # removidos — substituídos por reactions nativas do Telegram (count
    # via @dp.message_reaction + tabela track_reactions). Layout +clean.
    # NOTA: total_likes/liked/plays_source ainda calculados acima pra
    # preservar compatibilidade com `register_play` (side effect) e o
    # ♥ user_total_likes da linha 1 (legacy, dados históricos).
    _ = (total_likes, liked)  # mantém vars pra clareza/grep
    caption = (
        f"<b><a href=\"{html.escape(user_link)}\">{display_name}</a></b> · ♥ <code>{user_total_likes}</code>\n\n"
        f"♫ <code>{total_plays}</code> · <b>{track_part}</b> — <i>{artist}</i>"
    )
    # Sprint 10: emoji vai pro 5º slot do tuple — callers usam pra
    # set_message_reaction depois de enviar o card.
    card_emoji = _pick_card_emoji(total_plays, plays_source)
    return track_id, caption, cover, None, card_emoji


async def build_playing_payload(
    message: Message, track: dict
) -> tuple[str, str, str | None, None, str] | None:
    """Registra o play e monta (track_id, caption HTML, cover_url, keyboard).

    Side effect: chama `likes_service.register_play`. Retorna `None` se faltar
    `from_user` ou `track_id`. Reaproveitado por /playing e /tcanvas pra
    garantir mesma legenda + mesmos botões.
    """
    if not message.from_user:
        return None
    return await build_playing_payload_for_user(
        message.from_user.id,
        message.from_user.full_name or "Usuário",
        track,
    )


async def build_tly_payload(
    message: Message, track: dict, lyric_snippet: str | None
) -> tuple[str, str, str | None, str] | None:
    """Monta o payload do /tly: cabeçalho enxuto + quote expansível da letra.

    Mesma infra do /playing (registra a play e resolve o contador ♫), mas a
    legenda é diferente: nome de exibição em negrito unicode · ♫ N · faixa —
    artista, seguido de um `<blockquote expandable>` com `lyric_snippet`. Sem
    a linha de ♥ likes. Quando `lyric_snippet` é None/vazio, sai só o
    cabeçalho. Retorna (track_id, caption HTML, cover_url, card_emoji) ou None
    se faltar `from_user`/`track_id`.

    Side effect: chama `likes_service.register_play` (igual ao /tcanvas).
    """
    if not message.from_user:
        return None
    user_id = message.from_user.id
    display_name_raw = message.from_user.full_name or "Usuário"

    track_id = str(track.get("track_id") or "").strip()
    if not track_id:
        return None

    track_name_raw = str(track.get("track_name") or "").strip()
    artist_raw = str(track.get("artist") or "").strip()
    try:
        await likes_service.register_play(user_id, track_id, track_name=track_name_raw, artist_name=artist_raw)
    except Exception:
        logger.exception("REGISTER_PLAY_FAILED user=%s track=%s", user_id, track_id)

    total_plays, plays_source = await _resolve_play_button_count(user_id, track_id, artist_raw, track_name_raw)

    track_name, artist, track_url, cover = _track_label(track)
    track_part = f'<a href="{track_url}">{track_name}</a>' if track_url else track_name
    safe_name = html.escape(display_name_raw or "Usuário")
    user_link = f"tg://user?id={user_id}"
    name_part = f'<b><a href="{html.escape(user_link, quote=True)}">{safe_name}</a></b>'
    header = f"{name_part} · ♫ {total_plays} · {track_part} — <i>{artist}</i>"
    if lyric_snippet:
        caption = f"{header}\n<blockquote expandable>{html.escape(lyric_snippet)}</blockquote>"
    else:
        caption = header

    card_emoji = _pick_card_emoji(total_plays, plays_source)
    return track_id, caption, cover, card_emoji



async def _send_playing(message: Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    if not is_user_connected(user_id):
        await message.answer(connect_hint_for(message.chat.type), parse_mode="HTML", disable_web_page_preview=True)
        return

    # U1: feedback nativo "enviando foto..." enquanto resolve track + monta payload.
    try:
        await message.bot.send_chat_action(message.chat.id, "upload_photo")
    except Exception:
        pass

    track = await music_service.get_current_or_last_played(user_id)
    if not track:
        await message.answer(
            "Nada está tocando agora. Bota algo pra rolar no Spotify ou Last.fm e tenta de novo.",
        )
        return

    payload = await build_playing_payload(message, track)
    if not payload:
        await message.answer("Erro ao identificar a música.")
        return
    track_id, caption, cover, keyboard, card_emoji = payload

    if cover:
        sent = await message.answer_photo(photo=cover, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    else:
        sent = await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)

    # Sprint 8: registra (chat_id, message_id) -> track pra que reactions
    # nativas dos users virem stats. Fire-and-forget interno (service
    # captura exceções), nunca quebra o envio do card.
    await reactions_service.register_card(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
        track_id=track_id,
        owner_user_id=user_id,
        track_name=str(track.get("track_name") or "").strip() or None,
        artist_name=str(track.get("artist") or "").strip() or None,
    )
    # Sprint 10: bot reage no próprio card (🔥 padrão, ❤ a cada 5 plays Last.fm).
    await _react_to_own_card(sent.bot, sent.chat.id, sent.message_id, card_emoji)


def _register_handlers(dp: Dispatcher) -> None:
    @dp.message(Command("start"))
    async def start(message: Message) -> None:
        # Sprint 9 (#3): deep links via /start <payload>.
        # Payloads suportados:
        #   - lastfm_<username> → tenta conectar Last.fm direto
        #     (URL: t.me/<bot>?start=lastfm_romastefale)
        #   - connect           → mostra fluxo de conexão (Last.fm + Spotify)
        #   - help              → atalho pra /help
        # Sem payload (ou payload desconhecido) → greeting padrão.
        # Validação: payload é alfanumérico + underscore, max 64 chars
        # (limite do próprio start_parameter do Telegram).
        parts = (message.text or "").split(maxsplit=1)
        payload = parts[1].strip() if len(parts) >= 2 else ""

        if payload.startswith("lastfm_") and message.from_user:
            raw_username = payload[len("lastfm_"):]
            mention = _user_mention(message)
            if not raw_username:
                await message.answer(
                    f"{mention}, link inválido (faltou o username).",
                    parse_mode="HTML",
                )
                return
            try:
                username, previous = await lastfm_service.set_username(
                    message.from_user.id, raw_username
                )
            except ValueError:
                await message.answer(
                    f"{mention}, username Last.fm inválido: "
                    f"<code>{html.escape(raw_username)}</code>",
                    parse_mode="HTML",
                )
                return
            if previous and previous.lower() == username.lower():
                head = f"{mention}, Last.fm reconfirmado: <b>@{html.escape(username)}</b>."
            elif previous:
                head = (
                    f"{mention}, atualizei seu Last.fm de "
                    f"<b>@{html.escape(previous)}</b> pra <b>@{html.escape(username)}</b>."
                )
            else:
                head = f"{mention}, Last.fm conectado: <b>@{html.escape(username)}</b>."
            await _answer_with_effect(message, head, _EFFECT_FIRE, parse_mode="HTML")
            return

        if payload == "connect":
            await message.answer(
                "🎧 <b>Conectar suas contas no tigraoRADIO</b>\n\n"
                "<b>Last.fm</b> (obrigatório pra extratos):\n"
                "<code>/lastfm seu_username</code> (sem @)\n\n"
                "<b>Spotify</b> (opcional, fallback):\n"
                "<code>/login</code>\n\n"
                "Depois disso, <code>/playing</code> mostra o que você está ouvindo.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        if payload == "help":
            # Redireciona pro mesmo conteúdo do /help.
            await help_command(message)  # type: ignore[name-defined]
            return

        # Greeting padrão contextual: DM comum ou grupo.
        await _answer_with_effect(
            message,
            _start_text(message),
            _EFFECT_PARTY,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        # Effect FIRE em DM; em grupo usa fallback normal. Conteúdo depende do escopo.
        await _answer_with_effect(
            message,
            _help_text(message),
            _EFFECT_FIRE,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @dp.message(Command("login"))
    async def login(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer(
                "🔒 Pra conectar suas contas, fala comigo no privado:\n"
                "1) <code>/login</code> — autoriza o Spotify\n"
                "2) <code>/lastfm seu_username</code> (sem o @) — conecta o Last.fm\n\n"
                "Sem isso você não aparece no /tnow nem usa /monthfm, /weekfm e cia.",
                parse_mode="HTML",
            )
            return
        if not message.from_user:
            return
        auth_url = spotify_service.build_auth_url(message.from_user.id)
        await message.answer(
            "🎧 <b>Conectando suas contas no tigraoRADIO</b>\n\n"
            f"1) <b>Spotify</b> — abre este link e autoriza:\n{auth_url}\n\n"
            "2) <b>Last.fm</b> — manda aqui:\n"
            "<code>/lastfm seu_username</code>  (sem o @)\n\n"
            "Só depois desses dois passos seu nome entra no /tnow e os comandos "
            "/monthfm, /weekfm, /playing funcionam pra você.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @dp.message(Command("logout"))
    async def logout(message: Message) -> None:
        if not message.from_user:
            return
        await spotify_service.clear_user_session(message.from_user.id)
        await message.answer("Spotify desconectado.")

    @dp.message(Command("lastfm"))
    async def lastfm(message: Message) -> None:
        if not message.from_user:
            return
        mention = _user_mention(message)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            current = await lastfm_service.get_username(message.from_user.id)
            if current:
                await message.answer(
                    f"{mention}, seu Last.fm salvo é <b>@{html.escape(current)}</b>.\n"
                    "Pra trocar: <code>/lastfm outro_username</code> (sem o @).\n"
                    "Pra desconectar: /lastfmoff",
                    parse_mode="HTML",
                )
            else:
                await message.answer(
                    f"{mention}, você ainda não conectou um Last.fm.\n\n"
                    "🎧 <b>Como conectar:</b>\n"
                    "1) Abre seu perfil no Last.fm: https://www.last.fm/\n"
                    "2) Copia só o <b>username</b> (o que vem depois de /user/, <b>sem o @</b>)\n"
                    "3) Manda aqui: <code>/lastfm seu_username</code>\n\n"
                    "Exemplo: se sua URL é <code>last.fm/user/romastefale</code>, "
                    "manda <code>/lastfm romastefale</code>.\n\n"
                    "Sem isso você não aparece no /tnow nem usa /monthfm e /weekfm.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            return
        try:
            username, previous = await lastfm_service.set_username(message.from_user.id, parts[1])
        except ValueError:
            await message.answer(f"{mention}, username Last.fm inválido.", parse_mode="HTML")
            return
        if previous and previous.lower() == username.lower():
            head = f"{mention}, Last.fm reconfirmado: <b>@{html.escape(username)}</b>."
        elif previous:
            head = (
                f"{mention}, atualizei seu Last.fm de <b>@{html.escape(previous)}</b> "
                f"pra <b>@{html.escape(username)}</b>."
            )
        else:
            head = f"{mention}, Last.fm conectado: <b>@{html.escape(username)}</b>."
        if not LASTFM_API_KEY:
            await message.answer(
                f"{head}\n\n"
                "A leitura do Last.fm precisa da variável LASTFM_API_KEY no Railway. "
                "Enquanto ela não existir, o bot continua usando Spotify como fallback.",
                parse_mode="HTML",
            )
            return
        # Sprint 9 (#8): conexão bem-sucedida ganha efeito 🔥 (DM only, fallback OK).
        await _answer_with_effect(message, head, _EFFECT_FIRE, parse_mode="HTML")

    @dp.message(Command("lastfmoff"))
    async def lastfmoff(message: Message) -> None:
        if not message.from_user:
            return
        mention = _user_mention(message)
        removed = await lastfm_service.clear_username(message.from_user.id)
        # Sprint 10: effect THUMBS_UP em DM (toda vez), graceful em grupo.
        await _answer_with_effect(
            message,
            f"{mention}, Last.fm removido." if removed else f"{mention}, nenhum Last.fm estava conectado.",
            _EFFECT_THUMBS_UP,
            parse_mode="HTML",
        )

    @dp.message(Command("playing"))
    async def playing(message: Message) -> None:
        await _send_playing(message)

    # /myself e /songcharts foram movidos pra `app/bot/myself.py` e
    # `app/bot/songcharts.py`. Os novos comandos usam Last.fm (em vez de
    # likes locais) e renderizam o mesmo card visual dos /weekfm e
    # /monthfm. O ranking de grupo (/songcharts) agrega todos os
    # membros conectados e fixa a mensagem.

    @dp.callback_query(F.data.startswith("plays:"))
    async def plays_callback(query: CallbackQuery) -> None:
        if not query.from_user or not query.data:
            return
        parts = query.data.split(":", 3)
        if len(parts) == 4:
            try:
                owner_user_id = int(parts[1])
            except ValueError:
                owner_user_id = query.from_user.id
            plays_source = parts[2]
            track_id = parts[3]
        elif len(parts) == 3:
            try:
                owner_user_id = int(parts[1])
            except ValueError:
                owner_user_id = query.from_user.id
            plays_source = "local"
            track_id = parts[2]
        else:
            owner_user_id = query.from_user.id
            plays_source = "local"
            track_id = query.data.split(":", 1)[1]
        count = await likes_service.get_user_play_count(owner_user_id, track_id)
        if plays_source == "lastfm":
            await query.answer("O número azul é o total do Last.fm.\nPelo bot: " + str(count) + " vez" + ("" if count == 1 else "es") + ".", show_alert=True)
        else:
            await query.answer(f"O dono já ouviu {count} vez" + ("" if count == 1 else "es") + " pelo bot.", show_alert=True)

    @dp.callback_query(F.data.startswith("like:"))
    async def like_callback(query: CallbackQuery) -> None:
        # Sprint 8: botão ♥ likes foi removido — substituído por reactions
        # nativas. Stub silencioso pra mensagens ANTIGAS (cards postados
        # antes do deploy) que ainda têm o botão pendurado. Sem isso, o
        # clique falharia com "callback expired" e o user veria erro.
        await query.answer("Agora curtir é só reagir na mensagem 👀")

    @dp.message_reaction()
    async def on_message_reaction(event: MessageReactionUpdated) -> None:
        """Sprint 8: tracking de reactions nos cards /playing.

        Telegram envia este update toda vez que um user adiciona/remove
        reaction. Se a mensagem for um card musical trackado
        (existe em card_messages), grava o diff em track_reactions.
        Caso contrário, ignora silenciosamente.
        """
        if not event.user:
            return  # reaction anônima (rara) ou de canal — ignora
        old_emojis = [
            r.emoji for r in (event.old_reaction or [])
            if hasattr(r, "emoji") and getattr(r, "emoji", None)
        ]
        new_emojis = [
            r.emoji for r in (event.new_reaction or [])
            if hasattr(r, "emoji") and getattr(r, "emoji", None)
        ]
        try:
            await reactions_service.apply_reaction_change(
                chat_id=event.chat.id,
                message_id=event.message_id,
                user_id=event.user.id,
                old_emojis=old_emojis,
                new_emojis=new_emojis,
            )
        except Exception:
            logger.exception(
                "MESSAGE_REACTION_FAILED chat=%s msg=%s user=%s",
                event.chat.id, event.message_id, event.user.id,
            )

    # Inline público. Query vazia (ou "playing") -> card da música tocando
    # como 1ª opção. Query com termo -> busca por termo (mesmo motor do /radiofm).
    def _is_x9_inline_format(query: InlineQuery) -> bool:
        parts = (query.query or "").strip().split()
        if len(parts) != 2:
            return False
        return all(p.lstrip("-").isdigit() for p in parts)

    def _is_music_inline_v2_format(query: InlineQuery) -> bool:
        try:
            from app.bot.music_inline import is_music_inline_query

            return is_music_inline_query(query.query)
        except Exception:
            logger.debug("MUSIC_INLINE_V2_FORMAT_CHECK_FAILED", exc_info=True)
            return False

    async def _answer_playing(query: InlineQuery) -> None:
        track = await music_service.get_current_or_last_played(query.from_user.id)
        if not track:
            await query.answer([], cache_time=1, is_personal=True)
            return
        track_name, artist, track_url, cover = _track_label(track)
        if not cover:
            await query.answer([], cache_time=1, is_personal=True)
            return
        who = html.escape(query.from_user.full_name or "Usuário")
        user_link = f"tg://user?id={query.from_user.id}"
        who_part = f'<b><a href="{html.escape(user_link, quote=True)}">{who}</a></b>'
        track_part = f'<a href="{track_url}">{track_name}</a>' if track_url else track_name
        caption = f"{who_part}\n\n♫ <b>{track_part}</b> — <i>{artist}</i>"
        result = InlineQueryResultPhoto(
            id=str(uuid.uuid4()),
            photo_url=cover,
            thumbnail_url=cover,
            caption=caption,
            parse_mode="HTML",
        )
        await query.answer([result], cache_time=2, is_personal=True)

    @dp.inline_query(lambda q: not _is_x9_inline_format(q) and not _is_music_inline_v2_format(q))
    async def inline_public(query: InlineQuery) -> None:
        raw = (query.query or "").strip()
        if not raw:
            await query.answer([], cache_time=1, is_personal=True)
            return
        if raw.lower() == "playing":
            await _answer_playing(query)
            return

        from app.services.track_search import search_tracks

        hits = await search_tracks(raw, limit=10)
        results: list[InlineQueryResultPhoto] = []
        for hit in hits:
            if not hit.cover_big:
                continue
            name_part = _inline_public_name_style(query.from_user.full_name or "Usuário")
            caption = f"{name_part}\n♫ {html.escape(hit.title)} — {html.escape(hit.artist)}"
            results.append(
                InlineQueryResultPhoto(
                    id=str(uuid.uuid4()),
                    photo_url=hit.cover_big,
                    thumbnail_url=hit.cover_thumb or hit.cover_big,
                    title=hit.title,
                    description=hit.artist,
                    caption=caption,
                    parse_mode="HTML",
                )
            )
        await query.answer(results, cache_time=5, is_personal=True)

    # IMPORTANTE: o filtro `~F.text.startswith("/")` impede que este handler
    # consuma comandos. Sem isso, qualquer texto começando com "/" (ex.:
    # /weekfm, /monthfm em sub-routers) bateria neste handler primeiro, o
    # `return` cedo devolveria None ao observer (que NÃO é UNHANDLED em
    # aiogram3), e a propagação para sub-routers seria abortada.
    # StateFilter(None) também evita interceptar texto durante FSM.
    # Music-only: sem diálogo privado com estado de espera; mantém comandos musicais livres.
    def _music_dialog_active(message: Message) -> bool:
        # Music-only build: não há diálogo privado com estado de espera.
        return False

    @dp.message(
        StateFilter(None),
        F.text,
        ~F.text.startswith("/"),
        lambda m: not _music_dialog_active(m),
    )
    async def text_aliases(message: Message):
        if not _should_handle_text_alias(message):
            return UNHANDLED
        # U3: alias textual para /playing.
        await _send_playing(message)


async def shutdown_telegram_bot() -> None:
    await spotify_service.shutdown()
