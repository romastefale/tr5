"""Identidade do bot (nome + foto de perfil) com cache de TTL.

Usado pelo card do /story para estampar o nome atual do bot e usar a foto
de perfil dele como ícone. Como o nome/foto mudam raramente, cacheia em
memória; quando a foto do bot muda, novos cards refletem a foto
nova assim que o cache expira (TTL).

Solução real da Bot API:
- nome: `getMe` -> first_name/username
- foto: `getUserProfilePhotos(<bot_id>)` -> maior PhotoSize -> `getFile` ->
  download dos bytes. Bots conseguem ler a própria foto de perfil.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aiogram import Bot

logger = logging.getLogger(__name__)

# 1h: equilíbrio entre não martelar a API a cada card e refletir uma troca
# de foto/nome do bot em tempo razoável.
_IDENTITY_TTL_SECONDS = 3600.0

# Fallback mínimo (efêmero) — NUNCA é cacheado. Só serve pra falha transitória
# de getMe sem cache prévio: a próxima chamada tenta de novo na hora.
_FALLBACK = None  # type: BotIdentity | None


@dataclass(frozen=True)
class BotIdentity:
    name: str
    username: str | None
    photo_bytes: bytes | None


_cache: BotIdentity | None = None
_cache_expires_at: float = 0.0
_refresh_lock = asyncio.Lock()


async def get_bot_identity(bot: Bot) -> BotIdentity:
    """Retorna nome + foto de perfil do bot, com cache de TTL.

    Nunca levanta. Só cacheia identidade vinda de um `getMe` bem-sucedido —
    falha transitória de getMe sem cache prévio devolve um fallback efêmero
    (não cacheado), pra próxima chamada tentar de novo na hora.
    """
    global _cache, _cache_expires_at, _FALLBACK
    now = time.monotonic()
    if _cache is not None and now < _cache_expires_at:
        return _cache

    # Lock evita refresh duplicado quando vários cards expiram o TTL juntos.
    async with _refresh_lock:
        now = time.monotonic()
        if _cache is not None and now < _cache_expires_at:
            return _cache

        try:
            me = await bot.get_me()
        except Exception:
            logger.warning("BOT_IDENTITY_GETME_FAILED", exc_info=True)
            if _cache is not None:
                return _cache  # cache velho > fallback mínimo
            if _FALLBACK is None:
                _FALLBACK = BotIdentity(name="bot", username=None, photo_bytes=None)
            return _FALLBACK  # efêmero, NÃO cacheia (sem renovar TTL)

        name = me.first_name or me.username or "bot"
        username = me.username
        photo_bytes: bytes | None = None
        try:
            photos = await bot.get_user_profile_photos(me.id, limit=1)
            if photos.total_count and photos.photos:
                sizes = photos.photos[0]
                if sizes:
                    largest = sizes[-1]  # último = maior resolução
                    file = await bot.get_file(largest.file_id)
                    if file.file_path:
                        buf = await bot.download_file(file.file_path)
                        photo_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
        except Exception:
            logger.warning("BOT_IDENTITY_PHOTO_FAILED", exc_info=True)

        identity = BotIdentity(name=name, username=username, photo_bytes=photo_bytes)
        _cache = identity
        _cache_expires_at = now + _IDENTITY_TTL_SECONDS
        return identity
