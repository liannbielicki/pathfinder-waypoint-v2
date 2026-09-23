import asyncio

import httpx
import pytest

from waypoint.worker import _supervise, make_anthropic

from .conftest import TEST_SETTINGS


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


def test_provider_client_owns_neither_retry_nor_the_default_timeout() -> None:
    # Constructed attributes, not call arguments: the point is what the client
    # actually does, however it comes to be built.
    #
    # max_retries=0 — the SDK's default 2 retries 429s invisibly underneath
    # llm.retry_rate_limit, which both triples the wall time of a call already
    # inside two app-level retry loops and delays the RateLimitExhausted that
    # tells an operator MAX_LLM_IN_FLIGHT is too high for the model tier.
    #
    # timeout — the SDK's 600s default read timeout let one logical call
    # outlive the worker's lease and get the job re-claimed and double-paid.
    client = make_anthropic(TEST_SETTINGS)
    assert client.max_retries == 0
    assert isinstance(client.timeout, httpx.Timeout)
    assert client.timeout.read == TEST_SETTINGS.LLM_TIMEOUT_SECONDS
    assert client.timeout.write == TEST_SETTINGS.LLM_TIMEOUT_SECONDS
    assert client.timeout.pool == TEST_SETTINGS.LLM_TIMEOUT_SECONDS
    assert client.timeout.connect == 10.0
