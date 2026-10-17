"""Billing each paid model call to the user it is made for (ADR 0003): the allowance check before the call, and the
charge after it, priced from the provider's token counts or, when those are missing, estimated from the text."""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

from fastapi import HTTPException
from learning_transport import failure

import openai_client
import spend
from ingest import costs
from settings import get_settings

log = logging.getLogger("ai-core.billing")

WRITE_GRACE = 10.0  # seconds shutdown waits for charges still being written


def ledger_unavailable() -> HTTPException:
    """503 when the usage ledger cannot be read."""
    return failure(503, "USAGE_LEDGER_UNAVAILABLE", "AI usage cannot be checked right now.")


class Billing:
    """The usage ledger, and the charges still being written to it."""

    def __init__(self, ledger: spend.Ledger) -> None:
        self.ledger = ledger
        self._writes: set[asyncio.Future[None]] = set()

    async def check(self, billed: spend.Billed) -> None:
        """Refuse before any paid work once the user's daily allowance is used up. Without a working ledger
        nothing runs: a call nobody can account for is not made."""
        try:
            usage = await self.ledger.today(billed)
        except Exception:
            log.error("ledger_unavailable")
            raise ledger_unavailable() from None
        if spend.over_limit(usage):
            raise spend.limit_reached(usage)

    def charge(self, billed: spend.Billed, correlation: str, *charges: spend.Charge) -> None:
        """Record what a call cost. The write runs as its own task so a caller hanging up (a closed stream)
        cannot cancel it; a failed write is logged, since the money is already spent."""
        if not charges:
            return

        async def write() -> None:
            for c in charges:
                try:
                    await self.ledger.record(billed, c, correlation)
                except Exception:
                    log.error("ledger_write_failed", extra={"operation": c.operation, "cost_usd": c.cost_usd})

        task = asyncio.ensure_future(write())
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def drain(self) -> None:
        """Wait, briefly, for charges still being written."""
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
    """A charge priced from token counts; `cached_in` of `tokens_in` are billed at the model's cached rate."""
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
    """A chat reply's charge: OpenAI's token counts (cached prompt tokens at the cached rate) when it sent them,
    otherwise an estimate from the text."""
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


@dataclass
class _ModelTokens:
    prompt: int = 0
    cached: int = 0
    completion: int = 0


def ingest_charges(record: costs.IngestCost) -> list[spend.Charge]:
    """An ingest's model calls, one charge per model: the vision passes and the chunk embeddings."""
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
    """Charges a streamed reply once, however it ends: with OpenAI's counts when the usage chunk arrived, or an
    estimate from the text when the stream broke or the caller hung up first. The estimate can only count the text
    relayed: reasoning tokens never appear in the stream, so with CHAT_REASONING_EFFORT above "none" (the default)
    a cut-off stream is under-charged by the reasoning the model did."""

    def __init__(
        self, billing: Billing, billed: spend.Billed, correlation: str, model: str, prompt: str
    ) -> None:
        self.billing, self.billed, self.correlation = billing, billed, correlation
        self.model, self.prompt = model, prompt
        self.text: list[str] = []
        self.done = False

    def close(self, usage: openai_client.Usage | None = None) -> None:
        if self.done:
            return
        self.done = True
        charge = reply_charge("stream", self.model, self.prompt, "".join(self.text), usage)
        self.billing.charge(self.billed, self.correlation, charge)
