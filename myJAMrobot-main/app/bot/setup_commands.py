from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from app.config.settings import CODE_OWNER_IDS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandDef:
    command: str
    description: str


_PUBLIC_COMMANDS: tuple[CommandDef, ...] = (
    CommandDef("start", "Início"),
    CommandDef("help", "Ajuda"),
    CommandDef("login", "Informar usuário da LAST FM"),
    CommandDef("playing", "Música atual"),
    CommandDef("canvas", "Canvas da música atual"),
    CommandDef("story", "Story vertical 9:16"),
    CommandDef("radio", "Buscar e enviar uma música"),
    CommandDef("lyrics", "Música com trecho de refrão"),
)
_OWNER_ONLY_COMMANDS: tuple[CommandDef, ...] = (CommandDef("onoff", "Ligar ou desligar o acesso público"),)
_ALL_COMMANDS = _PUBLIC_COMMANDS + _OWNER_ONLY_COMMANDS


def _telegram_commands(items: tuple[CommandDef, ...]) -> list[BotCommand]:
    return [BotCommand(command=item.command, description=item.description) for item in items]


def command_scope_summary() -> dict[str, object]:
    return {
        "all": [item.command for item in _ALL_COMMANDS],
        "public": [item.command for item in _PUBLIC_COMMANDS],
        "owner_only": [item.command for item in _OWNER_ONLY_COMMANDS],
    }


async def setup_bot_commands(bot: Bot) -> None:
    public = _telegram_commands(_PUBLIC_COMMANDS)
    owner = _telegram_commands(_ALL_COMMANDS)
    failures: list[str] = []
    scopes = (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats())
    for scope in scopes:
        try:
            await bot.delete_my_commands(scope=scope)
            await bot.set_my_commands(public, scope=scope)
        except Exception:
            logger.warning("BOT_COMMAND_SCOPE_UPDATE_FAILED scope=%s", type(scope).__name__)
            failures.append(type(scope).__name__)
    for owner_id in CODE_OWNER_IDS:
        scope = BotCommandScopeChat(chat_id=owner_id)
        try:
            await bot.delete_my_commands(scope=scope)
            await bot.set_my_commands(owner, scope=scope)
        except Exception:
            logger.warning("BOT_OWNER_COMMAND_SCOPE_UPDATE_FAILED owner_id=%s", owner_id)
            failures.append(f"owner:{owner_id}")
    if failures:
        raise RuntimeError(f"command scope update failed for {len(failures)} scope(s)")
