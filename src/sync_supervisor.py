"""Keep the Matrix sync loop running.

``mautrix``'s syncer retries transient sync errors internally, but a fatal
exception ends ``Client.start()`` and the task completes *silently* — the
process stays alive with a dead sync loop, which is exactly the "Up but dead"
failure mode we are trying to eliminate. This supervisor restarts the sync
loop with capped exponential backoff whenever it ends while the bot is meant
to be running.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


async def run_sync_forever(
    client,
    should_stop: Callable[[], bool],
    *,
    base_delay: float = 5.0,
    max_delay: float = 320.0,
    healthy_run: float = 60.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run ``client.start(None)`` until ``should_stop()`` is true.

    If the sync loop ends, restart it after a jittered backoff. A loop that ran
    for at least *healthy_run* seconds resets the backoff, so a one-off dropout
    does not accumulate delay.
    """
    attempt = 0
    while not should_stop():
        started = clock()
        try:
            await client.start(None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Sync task raised")

        if should_stop():
            logger.info("Sync loop stopped; shutting down")
            return

        ran_for = clock() - started
        attempt = 0 if ran_for >= healthy_run else attempt
        attempt += 1
        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay = delay / 2 + rand() * (delay / 2)
        logger.warning(
            "Sync loop ended after %.0fs; restarting in %.1fs (attempt %d)",
            ran_for,
            delay,
            attempt,
        )
        await sleep(delay)


__all__ = ["run_sync_forever"]
