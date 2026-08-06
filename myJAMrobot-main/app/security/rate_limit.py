from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque

from app.config.settings import (
    COMMAND_RATE_LIMIT_ENABLED,
    COMMAND_RATE_LIMIT_EXPENSIVE_PER_WINDOW,
    COMMAND_RATE_LIMIT_STANDARD_PER_WINDOW,
    COMMAND_RATE_LIMIT_WINDOW_SECONDS,
)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int = 0
    count: int = 0
    limit: int = 0


_EXPENSIVE_COMMANDS = {"playing", "canvas", "story", "radio", "lyrics"}
_BUCKETS: OrderedDict[tuple[str, int, int], Deque[datetime]] = OrderedDict()
_BOUND = 5000


def _limit(command: str) -> int:
    configured = COMMAND_RATE_LIMIT_EXPENSIVE_PER_WINDOW if command in _EXPENSIVE_COMMANDS else COMMAND_RATE_LIMIT_STANDARD_PER_WINDOW
    return max(1, int(configured))


def check_command_rate_limit(command: str, user_id: int, chat_id: int) -> RateLimitResult:
    if not COMMAND_RATE_LIMIT_ENABLED:
        return RateLimitResult(True)
    command = str(command or "").casefold().lstrip("/")
    now = datetime.now(timezone.utc)
    window = timedelta(seconds=max(1, COMMAND_RATE_LIMIT_WINDOW_SECONDS))
    key = (command, int(user_id), int(chat_id))
    queue = _BUCKETS.setdefault(key, deque())
    _BUCKETS.move_to_end(key)
    while queue and now - queue[0] >= window:
        queue.popleft()
    limit = _limit(command)
    if len(queue) >= limit:
        retry = max(1, int((window - (now - queue[0])).total_seconds()))
        return RateLimitResult(False, retry, len(queue), limit)
    queue.append(now)
    while len(_BUCKETS) > _BOUND:
        _BUCKETS.popitem(last=False)
    return RateLimitResult(True, count=len(queue), limit=limit)


async def enforce_message_rate_limit(message, command: str) -> bool:
    user, chat = getattr(message, "from_user", None), getattr(message, "chat", None)
    if not user or not chat:
        return False
    result = check_command_rate_limit(command, int(user.id), int(chat.id))
    if result.allowed:
        return True
    await message.answer(f"Aguarde {result.retry_after_seconds}s antes de tentar novamente.")
    return False


def reset_rate_limits() -> None:
    _BUCKETS.clear()


def rate_limit_status() -> dict[str, object]:
    return {"enabled": COMMAND_RATE_LIMIT_ENABLED, "buckets": len(_BUCKETS)}
