import asyncio

import pytest

from src.sync_supervisor import run_sync_forever


class FakeClient:
    """``start`` returns/raises according to ``behaviour(start_number)``."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.starts = 0

    async def start(self, filter_data):
        self.starts += 1
        action = self.behaviour(self.starts)
        if action == "raise":
            raise RuntimeError("sync died")
        if action == "cancel":
            raise asyncio.CancelledError()
        return None


def test_reconnects_with_backoff_after_sync_ends():
    client = FakeClient(lambda n: "stop")
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    asyncio.run(
        run_sync_forever(
            client,
            lambda: client.starts >= 3,
            base_delay=2.0,
            max_delay=320.0,
            sleep=sleep,
            rand=lambda: 0.0,
            clock=lambda: 0.0,
        )
    )

    assert client.starts == 3
    assert sleeps == [1.0, 2.0]


def test_long_run_resets_backoff():
    clock_state = {"t": 0.0}

    def clock():
        return clock_state["t"]

    async def start(filter_data):
        client.starts += 1
        clock_state["t"] += 100.0
        return None

    client = FakeClient(lambda n: "stop")
    client.start = start  # type: ignore[method-assign]

    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    asyncio.run(
        run_sync_forever(
            client,
            lambda: client.starts >= 3,
            base_delay=2.0,
            max_delay=320.0,
            healthy_run=60.0,
            sleep=sleep,
            rand=lambda: 0.0,
            clock=clock,
        )
    )

    assert sleeps == [1.0, 1.0]


def test_exceptions_from_start_do_not_propagate():
    client = FakeClient(lambda n: "raise")
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    asyncio.run(
        run_sync_forever(
            client,
            lambda: client.starts >= 2,
            sleep=sleep,
            rand=lambda: 0.0,
            clock=lambda: 0.0,
        )
    )

    assert client.starts == 2
    assert len(sleeps) == 1


async def _noop_sleep(delay):
    return None


def test_cancelled_error_propagates():
    client = FakeClient(lambda n: "cancel")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_sync_forever(
                client,
                lambda: False,
                sleep=_noop_sleep,
                rand=lambda: 0.0,
                clock=lambda: 0.0,
            )
        )
