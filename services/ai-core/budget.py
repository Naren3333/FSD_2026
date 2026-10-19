"""A request's shared reservation, grown before each paid provider attempt and settled by billing."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from uuid import uuid4

import httpx2
import openai
from fastapi import HTTPException
from learning_transport import failure

import spend
from settings import get_settings

current: ContextVar["Budget | None"] = ContextVar("budget", default=None)


class Budget:
    def __init__(self, ledger: spend.Ledger, billed: spend.Billed) -> None:
        self.ledger, self.billed = ledger, billed
        self.id = str(uuid4())
        self.failed_accounting = False
        self.uncertain = False

    async def start(self) -> None:
        await self.ledger.reserve(self.id, self.billed, 0.0, get_settings().user_concurrency)

    async def close(self) -> None:
        # A failed ledger write leaves the reservation held, conservatively, until reconciliation/day reset.
        if not self.failed_accounting and not self.uncertain:
            await self.ledger.release(self.id)


async def guarded[T](
    fn: Callable[[], Awaitable[T]], model: str, content: object, output: int = 0, *, image_tokens: int = 0
) -> T:
    active = current.get()
    reserved = 0.0
    if active is not None:
        # UTF-8 bytes bound ordinary text tokens conservatively; images get a separate explicit allowance.
        tokens = len(json.dumps(content, ensure_ascii=False).encode()) + 64 + image_tokens
        reserved = spend.cost(model, tokens, output)
        try:
            await active.ledger.reserve(active.id, active.billed, reserved, get_settings().user_concurrency)
        except HTTPException:
            raise
        except Exception:
            raise failure(503, "USAGE_LEDGER_UNAVAILABLE", "AI usage cannot be reserved right now.") from None
    try:
        result = await fn()
    except (openai.APITimeoutError, httpx2.TimeoutException, asyncio.CancelledError):
        if active:
            active.uncertain = True
            logging.getLogger("ai-core.budget").warning("provider_usage_uncertain")
        raise
    if active:
        usage = getattr(result, "usage", None)
        inputs = getattr(usage, "prompt_tokens", None)
        outputs = getattr(usage, "completion_tokens", 0) or 0
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        if isinstance(result, httpx2.Response) and result.is_success:
            try:
                inputs = result.json().get("usage", {}).get("total_tokens")
            except (ValueError, AttributeError):
                inputs = None
        if isinstance(inputs, int):
            actual = spend.cost(model, inputs, outputs, cached)
            try:
                await active.ledger.adjust(active.id, reserved - actual)
            except Exception:
                # Keep the conservative amount; final billing still records the known usage.
                logging.getLogger("ai-core.budget").warning("reservation_adjust_failed")
    return result
