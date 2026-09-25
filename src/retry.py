"""Bounded exponential backoff with jitter for async operations.

Used to make the Synapse-side startup steps (key sharing) survive the same
boot-race 503s that used to kill the MAS login. The login itself has a richer
HTTP-aware retry in :mod:`src.mas_login` (it inspects status codes and
``Retry-After``); this helper is for operations where every failure is retried.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)


async def retry_async(
    operation: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 10,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retry_on: Sequence[type[BaseException]] = (Exception,),
    description: str = "operation",
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> Any:
    """Run *operation* until it succeeds or the retry budget is exhausted.

    Retries on any exception in *retry_on* (except ``asyncio.CancelledError``,
    which is always re-raised). The final exception is re-raised once the budget
    is spent so the caller can fail loud.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay < 0 or max_delay < 0:
        raise ValueError("base_delay and max_delay must be >= 0")

    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except tuple(retry_on) as exc:
            last_error = exc

        if attempt >= max_attempts:
            break

        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay = delay / 2 + rand() * (delay / 2)
        logger.warning(
            "%s attempt %d/%d failed (%r); retrying in %.1fs",
            description,
            attempt,
            max_attempts,
            last_error,
            delay,
        )
        if on_retry is not None:
            on_retry(attempt, last_error)
        await sleep(delay)

    assert last_error is not None
    raise last_error


__all__ = ["retry_async"]
