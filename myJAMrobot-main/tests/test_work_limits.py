from __future__ import annotations

import asyncio

import pytest

from app.security.work_limits import BackgroundTaskPool


@pytest.mark.asyncio
async def test_background_pool_rejects_excess_and_recovers_capacity() -> None:
    pool = BackgroundTaskPool(2)
    release = asyncio.Event()
    started: list[str] = []

    async def worker(name: str) -> None:
        started.append(name)
        await release.wait()

    assert pool.submit("first", lambda: worker("first"))
    assert pool.submit("second", lambda: worker("second"))
    assert not pool.submit("third", lambda: worker("third"))
    assert not pool.submit("first", lambda: worker("duplicate"))
    await asyncio.sleep(0)
    assert pool.active == 2
    assert set(started) == {"first", "second"}

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if pool.active == 0:
            break
    assert pool.active == 0
    assert pool.submit("third", lambda: worker("third"))
    await asyncio.sleep(0)
    assert "third" in started
    await pool.shutdown()
