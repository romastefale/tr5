from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from app.config.settings import MAX_CONCURRENT_HEAVY_JOBS, MAX_LYRICS_BACKGROUND_TASKS

logger = logging.getLogger(__name__)


class BackgroundTaskPool:
    def __init__(self, maximum: int) -> None:
        self.maximum = max(1, int(maximum))
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def submit(self, key: str, factory: Callable[[], Awaitable[Any]]) -> bool:
        current = self._tasks.get(key)
        if current is not None and not current.done():
            return False
        if len(self._tasks) >= self.maximum:
            return False
        task = asyncio.create_task(factory(), name=f"myjam:{key[:40]}")
        self._tasks[key] = task

        def _done(completed: asyncio.Task[Any]) -> None:
            self._tasks.pop(key, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("BACKGROUND_TASK_FAILED key=%s", key)

        task.add_done_callback(_done)
        return True

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def active(self) -> int:
        return len(self._tasks)


_heavy_semaphore = asyncio.Semaphore(MAX_CONCURRENT_HEAVY_JOBS)


@asynccontextmanager
async def heavy_job_slot(*, wait_seconds: float = 0.05):
    acquired = False
    try:
        await asyncio.wait_for(_heavy_semaphore.acquire(), timeout=max(0.01, wait_seconds))
        acquired = True
    except asyncio.TimeoutError:
        pass
    try:
        yield acquired
    finally:
        if acquired:
            _heavy_semaphore.release()


lyrics_task_pool = BackgroundTaskPool(MAX_LYRICS_BACKGROUND_TASKS)
