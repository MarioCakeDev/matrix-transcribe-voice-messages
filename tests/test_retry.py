import asyncio

import pytest

from src.retry import retry_async


def _noop_sleep(sleeps):
    async def _sleep(delay):
        sleeps.append(delay)

    return _sleep


def test_success_on_first_attempt_does_not_retry():
    calls = []

    async def op():
        calls.append(1)
        return "ok"

    sleeps = []
    result = asyncio.run(retry_async(op, sleep=_noop_sleep(sleeps), rand=lambda: 0.5))

    assert result == "ok"
    assert len(calls) == 1
    assert sleeps == []


def test_retries_then_succeeds():
    calls = []

    async def op():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")
        return "ok"

    sleeps = []
    result = asyncio.run(
        retry_async(
            op,
            max_attempts=5,
            base_delay=1.0,
            max_delay=60.0,
            sleep=_noop_sleep(sleeps),
            rand=lambda: 0.5,
        )
    )

    assert result == "ok"
    assert len(calls) == 3
    assert sleeps == [0.75, 1.5]


def test_exhausted_retries_reraise_last_error():
    async def op():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(retry_async(op, max_attempts=3, sleep=_noop_sleep([]), rand=lambda: 0.5))


def test_cancelled_error_is_not_retried():
    calls = []

    async def op():
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(retry_async(op, max_attempts=3, sleep=_noop_sleep([])))

    assert len(calls) == 1


def test_on_retry_callback_receives_attempt_and_error():
    seen = []

    async def op():
        raise RuntimeError("x")

    def on_retry(attempt, exc):
        seen.append((attempt, type(exc).__name__))

    with pytest.raises(RuntimeError):
        asyncio.run(
            retry_async(
                op,
                max_attempts=3,
                sleep=_noop_sleep([]),
                rand=lambda: 0.5,
                on_retry=on_retry,
            )
        )

    assert seen == [(1, "RuntimeError"), (2, "RuntimeError")]


def test_backoff_is_capped():
    async def op():
        raise RuntimeError("x")

    sleeps = []
    with pytest.raises(RuntimeError):
        asyncio.run(
            retry_async(
                op,
                max_attempts=5,
                base_delay=1.0,
                max_delay=3.0,
                sleep=_noop_sleep(sleeps),
                rand=lambda: 1.0,
            )
        )

    assert sleeps == [1.0, 2.0, 3.0, 3.0]
    assert all(delay <= 3.0 for delay in sleeps)


def test_invalid_max_attempts_raises():
    async def op():
        return None

    with pytest.raises(ValueError):
        asyncio.run(retry_async(op, max_attempts=0))
