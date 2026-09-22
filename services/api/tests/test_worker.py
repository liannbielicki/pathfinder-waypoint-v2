import asyncio

import pytest

from waypoint.worker import _supervise


async def test_supervisor_restarts_a_crashed_service_loop() -> None:
    attempts = 0
    restarted = asyncio.Event()
    delays: list[float] = []

    async def child() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient database failure")
        restarted.set()
        await asyncio.Event().wait()

    async def sleep(delay: float) -> None:
        delays.append(delay)

    task = asyncio.create_task(_supervise("test child", child, sleep=sleep))
    await asyncio.wait_for(restarted.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == 2
    assert delays == [2.0]


async def test_supervisor_caps_exponential_restart_backoff() -> None:
    delays: list[float] = []

    async def child() -> None:
        raise RuntimeError("still unavailable")

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _supervise("test child", child, max_delay=4.0, sleep=sleep)
    assert delays == [2.0, 4.0, 4.0, 4.0]
