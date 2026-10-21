"""an ingest stops when its caller hangs up (content gives up after 20 minutes and retries later)."""

import asyncio
from typing import cast

import pytest
from fastapi import Request

import main


class FakeRequest:
    def __init__(self, gone_after: int) -> None:
        self.checks, self.gone_after = 0, gone_after

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.gone_after


def caller(gone_after: int) -> Request:
    """a request that reports its caller gone after `gone_after` checks; unless_disconnected uses nothing else."""
    return cast(Request, FakeRequest(gone_after))


@pytest.mark.anyio
async def test_work_finishing_first_returns_its_result() -> None:
    async def work() -> str:
        await asyncio.sleep(0.01)
        return "done"

    assert await main.unless_disconnected(caller(gone_after=100), work(), poll=0.005) == "done"


@pytest.mark.anyio
async def test_a_disconnected_caller_cancels_the_work() -> None:
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    with pytest.raises(main.CallerGoneError):
        await main.unless_disconnected(caller(gone_after=2), work(), poll=0.005)
    assert cancelled.is_set()


@pytest.mark.anyio
async def test_the_works_own_error_is_raised() -> None:
    async def work() -> str:
        raise ValueError("bad pdf")

    with pytest.raises(ValueError, match="bad pdf"):
        await main.unless_disconnected(caller(gone_after=100), work(), poll=0.005)
