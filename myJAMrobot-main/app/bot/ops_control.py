from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config.settings import is_code_owner
from app.security.rate_limit import enforce_message_rate_limit
from app.services.ops_control import set_silent_mode, silent_mode_enabled

router = Router(name="onoff")
_ALLOWED_WHILE_OFF = {"start", "help"}


def _user_id(event: TelegramObject) -> int | None:
    user = getattr(event, "from_user", None)
    if user is None and isinstance(event, CallbackQuery):
        user = event.from_user
    try:
        return int(user.id) if user else None
    except (TypeError, ValueError):
        return None


def _command(event: TelegramObject) -> str | None:
    text = str(getattr(event, "text", "") or "").strip()
    if not text.startswith("/"):
        return None
    return text[1:].split(maxsplit=1)[0].split("@", 1)[0].casefold()


class OperationalControlMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _user_id(event)
        if silent_mode_enabled() and not is_code_owner(user_id):
            if _command(event) not in _ALLOWED_WHILE_OFF:
                return None
        return await handler(event, data)


def install_operational_control_middleware(dispatcher: Dispatcher) -> None:
    if getattr(dispatcher, "_myjam_onoff_middleware", False):
        return
    middleware = OperationalControlMiddleware()
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
    setattr(dispatcher, "_myjam_onoff_middleware", True)


@router.message(Command("onoff"))
async def onoff_command(message: Message, command: CommandObject) -> None:
    if not message.from_user or not await enforce_message_rate_limit(message, "onoff"):
        return
    if not is_code_owner(message.from_user.id):
        await message.answer("Comando restrito ao proprietário.")
        return
    argument = (command.args or "").strip().casefold()
    if argument == "status":
        enabled = not silent_mode_enabled()
    elif argument in {"on", "ligar"}:
        enabled = True
        set_silent_mode(False, owner_user_id=message.from_user.id)
    elif argument in {"off", "desligar"}:
        enabled = False
        set_silent_mode(True, owner_user_id=message.from_user.id)
    elif not argument:
        new_silent = not silent_mode_enabled()
        set_silent_mode(new_silent, owner_user_id=message.from_user.id)
        enabled = not new_silent
    else:
        await message.answer("Use /onoff, /onoff on, /onoff off ou /onoff status.")
        return
    await message.answer("Acesso público ligado." if enabled else "Acesso público desligado; /start e /help continuam disponíveis.")
