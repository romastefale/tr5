from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
    Message,
)

from app.security.media import validate_media_url
from app.services.cover_cache import cover_cache_service
from app.services.lyrics import LyricExcerpt, bound_excerpt_text

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CardDelivery:
    message: Message
    mode: str
    photo: str | bytes | None


def _safe(value: object, maximum: int = 300) -> str:
    return html.escape(str(value or "").strip()[:maximum])


def _safe_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(char) < 32 for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or port not in {None, 443} or parsed.username or parsed.password:
        return None
    if parsed.hostname not in {"open.spotify.com", "www.last.fm", "www.deezer.com"}:
        return None
    return html.escape(raw, quote=True)


def music_caption_html(
    track: dict[str, Any],
    *,
    user_id: int,
    user_name: str,
    lyric: LyricExcerpt | None = None,
    lyric_status: str | None = None,
    footer: str = "Dados musicais: LAST FM",
) -> str:
    title = _safe(track.get("track_name"))
    artist = _safe(track.get("artist"))
    track_url = _safe_url(track.get("spotify_url"))
    title_part = f'<a href="{track_url}"><b>{title}</b></a>' if track_url else f"<b>{title}</b>"
    user = _safe(user_name or "Usuário", 100)
    user_link = f"tg://user?id={int(user_id)}"
    lines = [f'<a href="{user_link}"><b>{user}</b></a>', f"{title_part} — <i>{artist}</i>"]
    if lyric:
        source_name = "LRCLIB" if lyric.source == "lrclib" else "lyrics.ovh"
        lyric_text = bound_excerpt_text(lyric.text) or ""
        lines.extend(
            [
                f"<blockquote>{html.escape(lyric_text)}</blockquote>",
                f'{html.escape(lyric.label)} · <a href="{html.escape(lyric.source_url, quote=True)}">{source_name}</a> · até 10 palavras',
            ]
        )
    elif lyric_status:
        lines.append(f"<i>{_safe(lyric_status, 120)}</i>")
    lines.append(_safe(footer, 160))
    return "\n".join(lines)


def music_rich_message(
    track: dict[str, Any],
    *,
    user_id: int,
    user_name: str,
    photo: str | bytes | None,
    lyric: LyricExcerpt | None = None,
    lyric_status: str | None = None,
    footer: str = "Dados musicais: LAST FM",
) -> InputRichMessage:
    title = _safe(track.get("track_name"))
    artist = _safe(track.get("artist"))
    track_url = _safe_url(track.get("spotify_url"))
    title_part = f'<a href="{track_url}"><b>{title}</b></a>' if track_url else f"<b>{title}</b>"
    user = _safe(user_name or "Usuário", 100)
    blocks = [
        "<h2>playing</h2>",
        f'<p><a href="tg://user?id={int(user_id)}"><b>{user}</b></a></p>',
    ]
    media: list[InputRichMessageMedia] | None = None
    if photo:
        rich_photo = BufferedInputFile(photo, filename="cover.jpg") if isinstance(photo, bytes) else photo
        media = [InputRichMessageMedia(id="cover", media=InputMediaPhoto(media=rich_photo))]
        blocks.append('<figure><img src="tg://photo?id=cover"/></figure>')
    blocks.append(f"<p>{title_part}<br><i>{artist}</i></p>")
    if lyric:
        source_name = "LRCLIB" if lyric.source == "lrclib" else "lyrics.ovh"
        lyric_text = bound_excerpt_text(lyric.text) or ""
        blocks.append(f"<blockquote>{html.escape(lyric_text)}<cite>{html.escape(lyric.label)}</cite></blockquote>")
        blocks.append(
            f'<footer><a href="{html.escape(lyric.source_url, quote=True)}">{source_name}</a> · até 10 palavras</footer>'
        )
    elif lyric_status:
        blocks.append(f"<p><mark>{_safe(lyric_status, 120)}</mark></p>")
    blocks.append(f"<footer>{_safe(footer, 160)}</footer>")
    return InputRichMessage(html="\n".join(blocks), media=media, skip_entity_detection=True)


async def send_rich_text(message: Message, *, rich_html: str, fallback_html: str | None = None) -> Message:
    try:
        return await message.bot.send_rich_message(
            chat_id=message.chat.id,
            rich_message=InputRichMessage(html=rich_html, skip_entity_detection=True),
        )
    except Exception:
        logger.info("RICH_TEXT_FALLBACK")
        return await message.answer(fallback_html or rich_html, parse_mode="HTML", disable_web_page_preview=True)


async def send_music_card(
    bot: Bot,
    *,
    chat_id: int,
    track: dict[str, Any],
    user_id: int,
    user_name: str,
    lyric: LyricExcerpt | None = None,
    lyric_status: str | None = None,
    footer: str = "Dados musicais: LAST FM",
) -> CardDelivery:
    track_id = str(track.get("track_id") or "").strip() or None
    cover_url = validate_media_url(track.get("album_image_url"), kind="cover")
    photo = await cover_cache_service.resolve_photo(
        bot,
        track_id=track_id,
        cover_url=cover_url,
        filename="cover-hd.jpg",
    ) if cover_url else None
    rich = music_rich_message(
        track,
        user_id=user_id,
        user_name=user_name,
        photo=photo,
        lyric=lyric,
        lyric_status=lyric_status,
        footer=footer,
    )
    try:
        sent = await bot.send_rich_message(chat_id=chat_id, rich_message=rich)
        return CardDelivery(sent, "rich", photo)
    except Exception:
        logger.info("RICH_MUSIC_CARD_FALLBACK track_id=%s", track_id or "none")

    caption = music_caption_html(
        track,
        user_id=user_id,
        user_name=user_name,
        lyric=lyric,
        lyric_status=lyric_status,
        footer=footer,
    )
    if photo:
        fallback_photo = BufferedInputFile(photo, filename="cover-hd.jpg") if isinstance(photo, bytes) else photo
        try:
            sent = await bot.send_photo(chat_id=chat_id, photo=fallback_photo, caption=caption, parse_mode="HTML")
            return CardDelivery(sent, "photo", photo)
        except Exception:
            logger.info("PHOTO_MUSIC_CARD_FALLBACK track_id=%s", track_id or "none")
    sent = await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", disable_web_page_preview=True)
    return CardDelivery(sent, "text", photo)


async def edit_music_card(
    delivery: CardDelivery,
    *,
    track: dict[str, Any],
    user_id: int,
    user_name: str,
    lyric: LyricExcerpt | None,
    lyric_status: str | None,
) -> None:
    if delivery.mode == "rich":
        rich = music_rich_message(
            track,
            user_id=user_id,
            user_name=user_name,
            photo=delivery.photo,
            lyric=lyric,
            lyric_status=lyric_status,
        )
        try:
            await delivery.message.edit_text(text=None, rich_message=rich)
        except Exception:
            # Re-uploading embedded media during an edit can fail on older
            # clients/proxies. Preserve the requested same-message update by
            # retrying as plain HTML without an attachment.
            logger.info("RICH_MUSIC_EDIT_FALLBACK")
            caption = music_caption_html(
                track,
                user_id=user_id,
                user_name=user_name,
                lyric=lyric,
                lyric_status=lyric_status,
            )
            await delivery.message.edit_text(caption, parse_mode="HTML", disable_web_page_preview=True)
        return
    caption = music_caption_html(
        track,
        user_id=user_id,
        user_name=user_name,
        lyric=lyric,
        lyric_status=lyric_status,
    )
    if delivery.mode == "photo":
        await delivery.message.edit_caption(caption=caption, parse_mode="HTML")
    else:
        await delivery.message.edit_text(caption, parse_mode="HTML", disable_web_page_preview=True)
