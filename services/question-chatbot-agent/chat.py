"""One chat turn, ported from fsd_test's assistant: rewrite the question, search the class materials with the
user's own token, and stream a cited answer from ai-core.

Everything that can fail with a plain HTTP status (the user, the body, the thread's owner, the database) happens
before the stream starts, and the user's message is saved then. After that, failures arrive as an `error` event,
and the assistant message is saved whatever happens: `answered`, `no_results` (the fixed refusal), `error` with
the partial text, or `interrupted` when the client goes away.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from fastapi.responses import StreamingResponse
from learning_transport import UpstreamError, sse_event
from starlette.types import Send

import prompts
from citations import CitationRewriter
from clients import Done, Services
from config import Settings
from contracts import MAX_MESSAGE
from store import Store

log = logging.getLogger("question-chatbot-agent.chat")

HISTORY_TURNS = 6
REWRITE_MAX_TOKENS = 300
CITATION_FIELDS = (
    "chunk_id",
    "document_id",
    "title",
    "page_start",
    "page_end",
    "kind",
    "source",
    "content",
    "image",
)
MAX_DOCUMENTS_CHARS = 55000  # ai-core takes messages of up to 60000 characters
PASSAGE_OVERHEAD = 200  # characters each passage adds to the prompt besides its content: tag, title, pages


class SSEResponse(StreamingResponse):
    """Closes the body generator as soon as streaming stops, disconnects included, so its cleanup runs then rather
    than whenever the generator is garbage-collected."""

    def __init__(self, content: AsyncIterator[str]) -> None:
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def stream_response(self, send: Send) -> None:
        async with aclosing(self.body_iterator):  # type: ignore[type-var]
            await super().stream_response(send)


def error_of(e: UpstreamError) -> tuple[str, str]:
    """The `error` event for a failed upstream call: a code the browser can act on and a safe message."""
    if e.code == "AI_SPEND_LIMIT":
        return "AI_SPEND_LIMIT", "You've used today's AI allowance."
    if e.service == "content":
        if e.status in (401, 403):
            return "FORBIDDEN", "You no longer have access to this classroom's materials."
        return "SEARCH_UNAVAILABLE", "Searching the class materials failed. Try again later."
    if e.service == "keycloak" or e.status in (0, 503):
        return "AI_UNAVAILABLE", "The assistant is not available right now."
    return "AI_PROVIDER_FAILED", "The model could not answer. Try again."


@dataclass(frozen=True)
class TurnRequest:
    """What the user asked, in which thread, and on whose behalf."""

    thread: dict[str, Any]
    authorization: str  # the user's own bearer token, for Content search
    correlation: str
    content: str
    history: list[dict[str, str]]
    billed_to: dict[str, str]  # whose daily AI allowance pays for this turn's model calls


class Turn:
    """One answer: runs the pipeline, yields SSE frames and saves the assistant message."""

    def __init__(
        self,
        services: Services,
        store: Store,
        settings: Settings,
        pending: set[asyncio.Task[Any]],
        request: TurnRequest,
    ) -> None:
        self.services, self.store, self.settings, self.pending = services, store, settings, pending
        self.billed_to = request.billed_to
        self.thread, self.authorization, self.correlation = (
            request.thread,
            request.authorization,
            request.correlation,
        )
        self.content, self.history = request.content, request.history
        self.started = time.perf_counter()
        self.parts: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.rewriter: CitationRewriter | None = None
        self.version: str | None = None
        self.model: str | None = None
        self.usage_tokens: int | None = None
        self.outcome = "answered"
        self.rewritten_query: str | None = None  # None when the rewrite failed
        self.is_on_topic: bool | None = None
        self.small_talk = False
        self.searched = False
        self.top_score: float | None = None

    async def events(self) -> AsyncIterator[str]:
        error: tuple[str, str] | None = None
        resets_at: str | None = None
        try:
            async for frame in self._answer():
                yield frame
        except (asyncio.CancelledError, GeneratorExit):
            # The client went away. Saved in a task of its own: this one is being cancelled, and any await here
            # would be cancelled too.
            log.info("thread %s: client disconnected; saving the partial answer", self.thread["id"])
            self._save_detached("interrupted")
            raise
        except UpstreamError as e:
            log.warning("thread %s: %s", self.thread["id"], e)
            error, resets_at = error_of(e), e.resets_at
        except Exception:
            log.exception("thread %s: chat turn failed", self.thread["id"])
            error = ("INTERNAL_ERROR", "The answer failed.")
        # Saved before the last events, so a disconnect while they are sent can't lose the row.
        if error is None:
            message_id = await self._save(self.outcome)
            yield sse_event("citations", {"citations": self._citations()})
            yield sse_event("done", {"message_id": message_id, "outcome": self.outcome})
        else:
            await self._save("error")
            data: dict[str, Any] = {"code": error[0], "message": error[1]}
            if resets_at:
                data["resets_at"] = resets_at
            yield sse_event("error", data)

    async def _answer(self) -> AsyncIterator[str]:
        """Small talk gets a short reply without documents; an off-topic message, no results, or a top score under
        the floor gets the fixed refusal without a model call; anything else is answered from the results."""
        role = self.thread["role"]
        await self._rewrite()
        if self.small_talk:
            self.version = prompts.SMALL_TALK
            messages = prompts.build_small_talk_messages(role, self.history, self.content)
            tag_map: dict[str, str] = {}  # no documents, so any tag the model writes is dropped
        else:
            if self.is_on_topic is not False:
                self.results, fallback = await self.services.search(
                    self.authorization, str(self.thread["classroom_id"]), self.rewritten_query or self.content,
                    self.correlation,
                )  # fmt: skip
                self.searched = True
                if fallback and self.results:
                    # Without the reranker the results are in RRF order and the floor can't be applied, so an
                    # answer would rest on unchecked passages. Fail the turn instead; the user can ask again.
                    raise UpstreamError("content", 503, "RERANK_UNAVAILABLE")
                self.top_score = max((float(r["score"]) for r in self.results), default=None)
            if (
                self.is_on_topic is False
                or self.top_score is None
                or self.top_score < self.settings.relevance_floor
            ):
                self.outcome = "no_results"
                reply = prompts.refusal(role)
                self.parts.append(reply)
                yield sse_event("token", {"text": reply})
                return
            messages, self.version, tag_map = prompts.build_messages(
                role, self._fitting(), self.history, self.content
            )
        self.rewriter = CitationRewriter(tag_map)
        async for item in self.services.stream(
            messages, self.settings.answer_max_tokens, self.correlation, self.billed_to
        ):
            if isinstance(item, Done):
                self.model, self.usage_tokens = item.model, item.usage_tokens
            elif out := self.rewriter.feed(item):
                self.parts.append(out)
                yield sse_event("token", {"text": out})
        if out := self.rewriter.flush():
            self.parts.append(out)
            yield sse_event("token", {"text": out})

    def _fitting(self) -> list[dict[str, Any]]:
        """The results, best first, that fit in one prompt message."""
        kept: list[dict[str, Any]] = []
        size = 0
        for r in self.results:
            size += len(r.get("content") or "") + PASSAGE_OVERHEAD
            if kept and size > MAX_DOCUMENTS_CHARS:
                break
            kept.append(r)
        self.results = kept
        return kept

    async def _rewrite(self) -> None:
        """A standalone search query and the two flags (query_rewrite.v1). If the model fails or its reply is
        unusable the turn goes on: the raw message is searched and only the floor can refuse. ai-core being down or
        not configured fails the turn, as the answer would too."""
        try:
            output = await self.services.generate(
                prompts.rewrite_messages(self.history, self.content),
                REWRITE_MAX_TOKENS,
                prompts.REWRITE_SCHEMA,
                prompts.QUERY_REWRITE,
                self.correlation,
                self.billed_to,
            )
            out = json.loads(output)
            query, on_topic, small_talk = (
                str(out["query"]),
                bool(out["is_on_topic"]),
                bool(out["is_small_talk"]),
            )
        except UpstreamError as e:
            if error_of(e)[0] in ("AI_UNAVAILABLE", "AI_SPEND_LIMIT"):
                raise
            log.warning(
                "thread %s: query rewrite failed, searching the raw message: %s", self.thread["id"], e
            )
            return
        except (ValueError, KeyError, TypeError):
            log.warning("thread %s: unusable query rewrite, searching the raw message", self.thread["id"])
            return
        self.rewritten_query = query.strip()[:MAX_MESSAGE] or self.content
        self.is_on_topic, self.small_talk = on_topic, small_talk

    def _citations(self) -> list[dict[str, Any]]:
        """The cited chunks, in citation order. The rewriter only maps tags that were in the prompt, so each one is
        among the retrieved results."""
        if self.rewriter is None:
            return []
        by_id = {str(r["chunk_id"]): r for r in self.results}
        return [{k: by_id[cid].get(k) for k in CITATION_FIELDS} for cid in self.rewriter.cited]

    async def _save(self, outcome: str) -> str:
        if self.rewriter is not None and (held := self.rewriter.flush()):
            self.parts.append(held)  # an error or disconnect mid-tag: keep the text as written
        decided = self.searched or self.version is not None or outcome == "no_results"
        answer = {
            "content": "".join(self.parts),
            "outcome": outcome,
            "rewritten_query": self.rewritten_query,
            "is_on_topic": self.is_on_topic,
            "retrieved_chunk_ids": [str(r["chunk_id"]) for r in self.results] if decided else None,
            "citations": self._citations() if decided else None,
            "prompt_version": self.version,
            "model": self.model,
            "usage_tokens": self.usage_tokens,
            "latency_ms": round((time.perf_counter() - self.started) * 1000),
        }
        message_id = await self.store.save_answer(str(self.thread["id"]), answer)
        log.info(
            "chat_turn",
            extra={
                "thread_id": str(self.thread["id"]),
                "outcome": outcome,
                "prompt_version": self.version,
                "retrieved": len(self.results),
                "top_score": self.top_score,
                "cited": len(self.rewriter.cited if self.rewriter else []),
                "usage_tokens": self.usage_tokens,
                "latency_ms": answer["latency_ms"],
            },
        )
        return message_id

    def _save_detached(self, outcome: str) -> None:
        task = asyncio.get_running_loop().create_task(self._save(outcome))
        self.pending.add(task)  # a strong reference until it finishes; the lifespan waits for these
        task.add_done_callback(self.pending.discard)
        task.add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("saving an interrupted answer failed", exc_info=task.exception())
