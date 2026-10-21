"""what an ingest cost: model calls by kind, cache hits, failures, tokens, retries and wall time, plus the time each stage took."""

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openai_client

log = logging.getLogger("ai-core.ingest.costs")


@dataclass
class Calls:
    model: str | None = None
    calls: int = 0  # answered by the model
    hits: int = 0  # answered by the cache
    failed: int = 0  # gave up: the page, figure or table goes without
    retries: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0  # of prompt_tokens, served from openai's prompt cache
    completion_tokens: int = 0
    seconds: float = 0.0  # summed over calls, so concurrent calls add up to more than the wall time


@dataclass
class IngestCost:
    document_id: str
    started: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    title: str | None = None
    outcome: str | None = None  # indexed / failed / skipped (allowance) / abandoned / error
    failure_reason: str | None = None
    pages: int | None = None
    chunks: int | None = None
    embed_inputs: int = 0
    embed_tokens: int = 0  # the chunk embeddings, in tokens
    stages: dict[str, float] = field(default_factory=dict)  # seconds: queued, vision, parse, embed, total
    kinds: dict[str, Calls] = field(default_factory=dict)

    def of(self, kind: str) -> Calls:
        return self.kinds.setdefault(kind, Calls())

    def to_json(self) -> dict:
        out = asdict(self)
        out["stages"] = {k: round(v, 2) for k, v in self.stages.items()}
        for c in out["kinds"].values():
            c["seconds"] = round(c["seconds"], 2)
        return out


_record: ContextVar[IngestCost | None] = ContextVar("ingest_cost", default=None)
_kind: ContextVar[str | None] = ContextVar("ingest_cost_kind", default=None)


@contextmanager
def recording(document_id: str) -> Iterator[IngestCost]:
    """the ingest's record, current inside the block (and in the tasks it starts)."""
    record = IngestCost(document_id)
    token = _record.set(record)
    try:
        yield record
    finally:
        _record.reset(token)


def current() -> IngestCost | None:
    return _record.get()


def hit(kind: str, model: str | None, n: int = 1) -> None:
    if (r := _record.get()) is not None:
        c = r.of(kind)
        c.hits += n
        c.model = c.model or model


def failed(kind: str | None = None, n: int = 1) -> None:
    if (r := _record.get()) is not None and (kind := kind or _kind.get()):
        r.of(kind).failed += n


@contextmanager
def calling(kind: str) -> Iterator[None]:
    """the kind the model calls made inside are attributed to."""
    token = _kind.set(kind)
    try:
        yield
    finally:
        _kind.reset(token)


def answered(model: str, usage: Any, seconds: float, retries: int, kind: str | None = None) -> None:
    """one successful model call. `usage` is the response's usage object (or none)."""
    if (r := _record.get()) is None or not (kind := kind or _kind.get()):
        return
    c = r.of(kind)
    c.model = c.model or model
    c.calls += 1
    c.retries += retries
    c.seconds += seconds
    counts = openai_client.usage_of(usage)
    c.prompt_tokens += counts.tokens_in or 0
    c.completion_tokens += counts.tokens_out or 0
    c.cached_tokens += counts.cached_in


def retried(n: int = 1, kind: str | None = None) -> None:
    """retries of a call that then failed (a successful one reports its retries through `answered`)."""
    if (r := _record.get()) is not None and (kind := kind or _kind.get()):
        r.of(kind).retries += n


@contextmanager
def stage(name: str) -> Iterator[None]:
    start_ = time.monotonic()
    try:
        yield
    finally:
        if (r := _record.get()) is not None:
            r.stages[name] = r.stages.get(name, 0.0) + time.monotonic() - start_


def write(record: IngestCost, path: Path | None) -> None:
    """log the record, and append it to `path` (json lines) when one is set."""
    fields = record.to_json()
    log.info("ingest_cost", extra=fields)
    line = json.dumps(fields, separators=(",", ":"))
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        log.exception(
            "ingest_cost_not_appended", extra={"document_id": record.document_id, "path": str(path)}
        )
