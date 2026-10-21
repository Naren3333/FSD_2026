"""reserve allowance before paid calls and record provider usage afterward."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from learning_transport import failure

import budget
import openai_client
import spend
from ingest import costs
from ingest.chunker import encoding
from settings import get_settings

MESSAGE_OVERHEAD = 4  # tokens openai's chat format adds around each message

log = logging.getLogger("ai-core.billing")

WRITE_GRACE = 10.0  # seconds shutdown waits for charges still being written


def ledger_unavailable() -> HTTPException:
    """503 when the usage ledger cannot be read."""
    return failure(503, "USAGE_LEDGER_UNAVAILABLE", "AI usage cannot be checked right now.")


def busy() -> HTTPException:
    """429 when the user already has as many paid calls running as ai_user_concurrency allows."""
    return failure(429, "AI_BUSY", "Too many AI requests are running for you at once. Try again in a moment.")


class Slot:
    """one of a user's running paid calls. release() frees it; calling it again does nothing."""

    def __init__(self, running: dict[tuple[str, str], int], key: tuple[str, str]) -> None:
        self._running, self._key, self._held = running, key, True
        running[key] = running.get(key, 0) + 1

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        left = self._running[self._key] - 1
        if left:
            self._running[self._key] = left
        else:
            del self._running[self._key]


class Billing:
    """the usage ledger, the charges still being written to it, and each user's running paid calls."""

    def __init__(self, ledger: spend.Ledger) -> None:
        self.ledger = ledger
        self._writes: set[asyncio.Future[None]] = set()
        self._running: dict[tuple[str, str], int] = {}

    def claim(self, billed: spend.Billed) -> Slot:
        """an extra process-local slot; postgresql reservations enforce the shared limit across replicas."""
        key = (billed.org_id, billed.user_id)
        if self._running.get(key, 0) >= get_settings().user_concurrency:
            raise busy()
        return Slot(self._running, key)

    async def check(self, billed: spend.Billed) -> None:
        """refuse before any paid work once the user's daily allowance is used up."""
        try:
            usage = await self.ledger.today(billed)
        except Exception:
            log.error("ledger_unavailable")
            raise ledger_unavailable() from None
        if spend.over_limit(usage):
            raise spend.limit_reached(usage)

    async def charge(
        self,
        billed: spend.Billed,
        correlation: str,
        *charges: spend.Charge,
        reservation: budget.Budget | None = None,
    ) -> None:
        """record what a call cost before its response ends, so the caller's next call is checked against it."""
        if not charges:
            return
        active = reservation or budget.current.get()

        async def write() -> None:
            for c in charges:
                try:
                    if active:
                        await self.ledger.record_reserved(active.id, billed, c, correlation)
                    else:
                        await self.ledger.record(billed, c, correlation)
                except Exception:
                    if active:
                        active.failed_accounting = True
                    log.error("ledger_write_failed", extra={"operation": c.operation, "cost_usd": c.cost_usd})

        await self._unless_cancelled(write())

    async def charge_timeout(  # noqa: PLR0913 - explicit accounting context and bounded provider request
        self,
        billed: spend.Billed,
        correlation: str,
        operation: str,
        model: str,
        messages: Sequence[dict[str, Any]],
        max_tokens: int,
        reservation: budget.Budget | None = None,
    ) -> None:
        """record timeout_charge for a call the provider did not answer in time."""

        async def count_then_charge() -> None:
            await self.charge(
                billed,
                correlation,
                await timeout_charge(operation, model, messages, max_tokens),
                reservation=reservation,
            )

        await self._unless_cancelled(count_then_charge())
        active = reservation or budget.current.get()
        if active and not active.failed_accounting:
            active.uncertain = False  # the timeout's full output allowance was durably charged

    async def _unless_cancelled(self, work: Coroutine[Any, Any, None]) -> None:
        """run `work` as its own task, which drain() waits for, so cancelling the caller cannot cancel it."""
        task = asyncio.ensure_future(work)
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)
        await asyncio.shield(task)

    async def drain(self) -> None:
        """wait, briefly, for charges still being written."""
        if self._writes:
            await asyncio.wait(self._writes, timeout=WRITE_GRACE)


def priced(
    operation: str,
    model: str,
    tokens_in: int,
    tokens_out: int = 0,
    estimated: bool = False,
    cached_in: int = 0,
) -> spend.Charge:
    """a charge priced from token counts; `cached_in` of `tokens_in` are billed at the model's cached rate."""
    return spend.Charge(
        operation,
        model,
        tokens_in,
        tokens_out,
        spend.cost(model, tokens_in, tokens_out, cached_in),
        estimated,
    )


def reply_charge(
    operation: str, model: str, prompt: str, reply: str, usage: openai_client.Usage | None
) -> spend.Charge:
    """a chat reply's charge: openai's token counts (cached prompt tokens at the cached rate) when it sent them, otherwise an estimate from the text."""
    tokens_in = usage.tokens_in if usage else None
    tokens_out = usage.tokens_out if usage else None
    return priced(
        operation,
        model,
        spend.estimate_tokens(prompt) if tokens_in is None else tokens_in,
        spend.estimate_tokens(reply) if tokens_out is None else tokens_out,
        estimated=tokens_in is None or tokens_out is None,
        cached_in=usage.cached_in if usage and tokens_in is not None else 0,
    )


def prompt_tokens(messages: Sequence[dict[str, Any]]) -> int:
    """the prompt's tokens by the committed cl100k_base tokenizer, with each message's framing."""
    enc = encoding()
    return sum(
        len(enc.encode(str(m.get("content", "")), disallowed_special=())) + MESSAGE_OVERHEAD for m in messages
    )


async def timeout_charge(
    operation: str, model: str, messages: Sequence[dict[str, Any]], max_tokens: int
) -> spend.Charge:
    """a call the provider did not answer in time."""
    tokens_in = await asyncio.to_thread(prompt_tokens, messages)
    return priced(operation, model, tokens_in, max_tokens, estimated=True)


@dataclass
class _ModelTokens:
    prompt: int = 0
    cached: int = 0
    completion: int = 0


def ingest_charges(record: costs.IngestCost) -> list[spend.Charge]:
    """an ingest's model calls, one charge per model: the vision passes and the chunk embeddings."""
    by_model: defaultdict[str, _ModelTokens] = defaultdict(_ModelTokens)
    for calls in record.kinds.values():
        if calls.model and calls.calls:
            tokens = by_model[calls.model]
            tokens.prompt += calls.prompt_tokens
            tokens.cached += calls.cached_tokens
            tokens.completion += calls.completion_tokens
    out = [
        priced("ingest", model, t.prompt, t.completion, cached_in=t.cached) for model, t in by_model.items()
    ]
    if record.embed_tokens:
        model = get_settings().embedding_model
        out.append(priced("ingest", model, record.embed_tokens))
    return out


class StreamBill:
    """charges a streamed reply once, however it ends: with openai's counts when the usage chunk arrived, or an estimate from the text when the stream broke or the caller hung up first."""

    def __init__(
        self,
        billing: Billing,
        billed: spend.Billed,
        correlation: str,
        model: str,
        messages: Sequence[dict[str, Any]],
        max_tokens: int,
    ) -> None:
        self.billing, self.billed, self.correlation = billing, billed, correlation
        self.model, self.messages, self.max_tokens = model, messages, max_tokens
        self.prompt = "".join(str(m.get("content", "")) for m in self.messages)
        self.text: list[str] = []
        self.done = False
        self.reservation = budget.current.get()

    async def close(self, usage: openai_client.Usage | None = None) -> None:
        if self.done:
            return
        self.done = True
        charge = reply_charge("stream", self.model, self.prompt, "".join(self.text), usage)
        await self.billing.charge(self.billed, self.correlation, charge, reservation=self.reservation)

    async def timed_out(self) -> None:
        if self.done:
            return
        self.done = True
        await self.billing.charge_timeout(
            self.billed,
            self.correlation,
            "stream",
            self.model,
            self.messages,
            self.max_tokens,
            self.reservation,
        )
