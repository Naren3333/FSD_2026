"""One chat turn, ported from fsd_test's assistant: rewrite the question, search the class materials with the
user's own token, and stream a cited answer from ai-core.

A message about several topics is searched once whole and once per topic (the rewrite's sub_queries), and the
searches take turns filling the prompt, so no topic crowds out another; one answer then sees every passage and can
explain how they connect.

Everything that can fail with a plain HTTP status (the user, the body, the thread's owner, the database) happens
before the stream starts, and the user's message is saved then. After that, failures arrive as an `error` event,
and the assistant message is saved whatever happens: `answered`, `no_results` (the fixed refusal), `error` with
the partial text, or `interrupted` when the client goes away. Only a thread purged meanwhile (its classroom or its
user deleted) keeps nothing more; the stream then ends with a NOT_FOUND `error` event.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from fastapi.responses import StreamingResponse
from learning_transport import UpstreamError, sse_event
from starlette.types import Receive, Scope, Send

import prompts
from citations import CitationRewriter
from clients import Done, Services
from config import Settings
from contracts import MAX_MESSAGE
from store import Store
from token_count import count as token_bound

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
# The thread was deleted (its classroom or its user purged) while the answer was written: nothing more is saved.
THREAD_GONE = ("NOT_FOUND", "This conversation was deleted.")
MAX_DOCUMENTS_CHARS = 55000  # ai-core takes messages of up to 60000 characters
PASSAGE_OVERHEAD = 200  # characters each passage adds to the prompt besides its content: tag, title, pages
MAX_SUB_QUERIES = 3
MERGED_TOP_K = 12  # passages a split message is answered from (one search gives SEARCH_TOP_K)
PER_DOCUMENT = 5  # of those, at most this many from one document


class SSEResponse(StreamingResponse):
    """Closes the body generator as soon as streaming stops, disconnects included, so its cleanup runs then rather
    than whenever the generator is garbage-collected. `finish` runs however the response ends: a client gone before
    the body starts means the generator never runs, so its own cleanup can't."""

    def __init__(self, content: AsyncIterator[str], finish: Callable[[], None]) -> None:
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self.finish = finish

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.finish()

    async def stream_response(self, send: Send) -> None:
        async with aclosing(self.body_iterator):  # type: ignore[type-var]
            await super().stream_response(send)


def error_of(e: UpstreamError) -> tuple[str, str]:
    """The `error` event for a failed upstream call: a code the browser can act on and a safe message."""
    if e.code == "AI_SPEND_LIMIT":
        return "AI_SPEND_LIMIT", "You've used today's AI allowance."
    if e.code == "AI_BUSY":
        return "AI_BUSY", "You have too many AI requests running at once. Try again in a moment."
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
        # The ms each step took, for the chat_turn log: where a slow turn spent its time.
        self.timings: dict[str, int] = {}
        self.parts: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.rewriter: CitationRewriter | None = None
        self.version: str | None = None
        self.model: str | None = None
        self.usage_tokens: int | None = None
        self.outcome = "answered"
        self.rewritten_query: str | None = None  # None when the rewrite failed
        self.sub_queries: list[str] = []  # the topics of a message about several, each searched on its own
        self.topic_of: dict[str, list[str]] = {}  # A duplicate passage can support several topic searches.
        self.is_on_topic: bool | None = None
        self.small_talk = False
        self.searched = False
        self.top_score: float | None = None
        self.begun = False  # whether events() has started; finish() saves the turn when it never did

    def finish(self) -> None:
        """Saves the turn as `interrupted` if its events never started: the client left before the stream began."""
        if not self.begun:
            self.begun = True
            log.info("chat_client_gone_before_answer", extra={"thread_id": str(self.thread["id"])})
            self._save_detached("interrupted")

    async def events(self) -> AsyncIterator[str]:
        self.begun = True
        error: tuple[str, str] | None = None
        resets_at: str | None = None
        try:
            async for frame in self._answer():
                yield frame
        except (asyncio.CancelledError, GeneratorExit):
            # The client went away. Saved in a task of its own: this one is being cancelled, and any await here
            # would be cancelled too.
            log.info("chat_client_disconnected", extra={"thread_id": str(self.thread["id"])})
            self._save_detached("interrupted")
            raise
        except UpstreamError as e:
            log.warning(
                "chat_turn_upstream_failed", extra={"thread_id": str(self.thread["id"]), "error": str(e)}
            )
            error, resets_at = error_of(e), e.resets_at
        except Exception:
            log.exception("chat_turn_failed", extra={"thread_id": str(self.thread["id"])})
            error = ("INTERNAL_ERROR", "The answer failed.")
        # Saved before the last events, so a disconnect while they are sent can't lose the row, and shielded, so a
        # disconnect during the save can't either: the save carries on and the lifespan waits for it.
        # A save that fails, or finds the thread purged meanwhile, still ends the stream with an `error` event.
        if error is None:
            saved = await self._saved(self.outcome)
            if isinstance(saved, str):
                yield sse_event("citations", {"citations": self._citations()})
                yield sse_event("done", {"message_id": saved, "outcome": self.outcome})
                return
            error = saved
        elif await self._saved("error") == THREAD_GONE:
            error, resets_at = THREAD_GONE, None
        data: dict[str, Any] = {"code": error[0], "message": error[1]}
        if resets_at:
            data["resets_at"] = resets_at
        yield sse_event("error", data)

    async def _saved(self, outcome: str) -> str | tuple[str, str]:
        """The saved answer's id, or the `error` event's code and message when it could not be saved."""
        try:
            message_id = await asyncio.shield(self._save_detached(outcome))
        except Exception:  # logged by _log_failure
            return ("INTERNAL_ERROR", "The answer could not be saved.")
        return THREAD_GONE if message_id is None else message_id

    async def _answer(self) -> AsyncIterator[str]:
        """Small talk gets a short reply without documents; an off-topic message, no results, or a top score under
        the floor gets the fixed refusal without a model call; anything else is answered from the results."""
        role = self.thread["role"]
        step = time.perf_counter()
        await self._rewrite()
        step = self._took("rewrite_ms", step)
        if self.small_talk:
            self.version = prompts.SMALL_TALK
            messages = prompts.build_small_talk_messages(role, self.history, self.content)
            tag_map: dict[str, str] = {}  # no documents, so any tag the model writes is dropped
        else:
            if self.is_on_topic is not False:
                await self._search()
                step = self._took("search_ms", step)
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
            fitting = self._fitting()
            if not fitting:
                self.outcome = "no_results"
                reply = prompts.refusal(role)
                self.parts.append(reply)
                yield sse_event("token", {"text": reply})
                return
            messages, self.version, tag_map = prompts.build_messages(
                role, fitting, self.history, self.content, self.topic_of
            )
        self.rewriter = CitationRewriter(tag_map)
        async for item in self.services.stream(
            messages, self.settings.answer_max_tokens, self.correlation, self.billed_to
        ):
            if "first_token_ms" not in self.timings:
                self._took("first_token_ms", step)
            if isinstance(item, Done):
                self.model, self.usage_tokens = item.model, item.usage_tokens
            elif out := self.rewriter.feed(item):
                self.parts.append(out)
                yield sse_event("token", {"text": out})
        if out := self.rewriter.flush():
            self.parts.append(out)
            yield sse_event("token", {"text": out})
        # Structural coverage only: a citation's presence does not establish semantic entailment.
        cited_topics = {t for cid in self.rewriter.cited for t in self.topic_of.get(cid, [])}
        missing = [t for t in self.sub_queries if t not in cited_topics] if not self.small_talk else []
        if missing:
            # Rewrite output is untrusted. Labels must not introduce citation markers or markup.
            labels = [re.sub(r"[\[\]<>`*_]", "", " ".join(t.split()))[:200] for t in missing]
            notice = "\n\nCitation coverage is incomplete for: " + "; ".join(labels) + "."
            self.parts.append(notice)
            yield sse_event("token", {"text": notice})
            log.warning("chat_topic_citations_incomplete", extra={"topics": missing})
        self._took("answer_ms", step)

    def _took(self, name: str, since: float) -> float:
        """Records the ms since `since` as `name` and returns now, the next step's start."""
        now = time.perf_counter()
        self.timings[name] = round((now - since) * 1000)
        return now

    async def _search(self) -> None:
        """Searches the whole message and, when it spans topics, each topic, all at once, then merges what came
        back. Each search holds one of the user's AI_USER_CONCURRENCY slots in ai-core while it embeds or reranks,
        so a split turn holds up to 1 + MAX_SUB_QUERIES."""
        queries = [self.rewritten_query or self.content, *self.sub_queries]
        classroom = str(self.thread["classroom_id"])

        # Every search finishes before a failure is raised, so none is left running after the turn.
        async def lookup(q: str) -> list[dict[str, Any]]:
            results, fallback = await self.services.search(self.authorization, classroom, q, self.correlation)
            if fallback and results:
                raise UpstreamError("content", 503, "RERANK_UNAVAILABLE")
            return results

        if self.settings.adaptive_search and len(queries) > 1:
            primary = await lookup(queries[0])

            async def topic(q: str) -> list[dict[str, Any]]:
                terms = set(re.findall(r"[a-z0-9]+", q.lower())) - {
                    "the",
                    "a",
                    "an",
                    "of",
                    "in",
                    "to",
                    "and",
                    "for",
                }
                covered = [
                    r
                    for r in primary
                    if terms
                    and terms.issubset(
                        set(
                            re.findall(
                                r"[a-z0-9]+",
                                (str(r.get("title", "")) + " " + str(r.get("section_path", ""))).lower(),
                            )
                        )
                    )
                    and float(r["score"]) >= self.settings.relevance_floor
                ]
                return covered if covered else await lookup(q)

            outcomes = await asyncio.gather(*(topic(q) for q in queries[1:]), return_exceptions=True)
            searches = [primary]
        else:
            outcomes = await asyncio.gather(*(lookup(q) for q in queries), return_exceptions=True)
            searches = []
        self.searched = True
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
            searches.append(outcome)
        self.top_score = max((float(r["score"]) for rs in searches for r in rs), default=None)
        if len(searches) == 1:
            self.results = searches[0]
            return
        merged = merge(searches, self.settings.relevance_floor, self.settings.topic_floor)
        self.results = [r for r, _ in merged]
        # Attribute every qualifying topic leg, including passages taken first by the primary search.
        retained = {str(r["chunk_id"]) for r in self.results}
        self.topic_of = {}
        for topic_label, passages in zip(self.sub_queries, searches[1:], strict=True):
            for passage in passages:
                cid = str(passage["chunk_id"])
                if cid in retained and float(passage["score"]) >= self.settings.topic_floor:
                    labels = self.topic_of.setdefault(cid, [])
                    if topic_label not in labels:
                        labels.append(topic_label)

    def _fitting(self) -> list[dict[str, Any]]:
        """The results, best first, that fit in one prompt message."""
        kept: list[dict[str, Any]] = []
        size = 0
        # Count with the offline tokenizer and reserve space for markup, history and the current question.
        budget = max(
            0,
            self.settings.context_tokens
            - sum(token_bound(m["content"]) for m in self.history)
            - token_bound(self.content)
            - 1000,
        )
        # Give each requested topic a chance before spending the remaining budget on extra passages.
        priority: list[dict[str, Any]] = []
        represented: set[str] = set()
        for label in self.sub_queries:
            for r in self.results:
                cid = str(r["chunk_id"])
                if label in self.topic_of.get(cid, []) and cid not in represented:
                    priority.append(r)
                    represented.add(cid)
                    break
        candidates = priority + [r for r in self.results if str(r["chunk_id"]) not in represented]
        for r in candidates:
            content = r.get("content") or ""
            cost = token_bound(content) + token_bound(str(r.get("title") or "")) + PASSAGE_OVERHEAD
            if (
                size + cost > budget
                or sum(len(k.get("content") or "") + PASSAGE_OVERHEAD for k in kept)
                + len(content)
                + PASSAGE_OVERHEAD
                > MAX_DOCUMENTS_CHARS
            ):
                continue
            size += cost
            kept.append(r)
        kept_ids = {str(r["chunk_id"]) for r in kept}
        self.results = [r for r in self.results if str(r["chunk_id"]) in kept_ids]
        return self.results

    async def _rewrite(self) -> None:
        """A standalone search query, the two flags and the topics (query_rewrite.v2). If the model fails or its
        reply is unusable the turn goes on: the raw message is searched and only the floor can refuse. ai-core being
        down or not configured fails the turn, as the answer would too."""
        if self.settings.adaptive_rewrite and not needs_rewrite(self.content, self.history):
            self.rewritten_query = self.content
            self.is_on_topic = True  # Retrieval, never this heuristic, decides whether there is evidence.
            return
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
            if error_of(e)[0] in ("AI_UNAVAILABLE", "AI_SPEND_LIMIT", "AI_BUSY"):
                raise
            log.warning("query_rewrite_failed", extra={"thread_id": str(self.thread["id"]), "error": str(e)})
            return
        except (ValueError, KeyError, TypeError):
            log.warning("query_rewrite_unusable", extra={"thread_id": str(self.thread["id"])})
            return
        self.rewritten_query = query.strip()[:MAX_MESSAGE] or self.content
        self.is_on_topic, self.small_talk = on_topic, small_talk
        self.sub_queries = topics(out.get("sub_queries"), self.rewritten_query)

    def _citations(self) -> list[dict[str, Any]]:
        """The cited chunks, in citation order. The rewriter only maps tags that were in the prompt, so each one is
        among the retrieved results."""
        if self.rewriter is None:
            return []
        by_id = {str(r["chunk_id"]): r for r in self.results}
        return [{k: by_id[cid].get(k) for k in CITATION_FIELDS} for cid in self.rewriter.cited]

    async def _save(self, outcome: str) -> str | None:
        if self.rewriter is not None and (held := self.rewriter.flush()):
            self.parts.append(held)  # an error or disconnect mid-tag: keep the text as written
        decided = self.searched or self.version is not None or outcome == "no_results"
        answer = {
            "content": "".join(self.parts),
            "outcome": outcome,
            "rewritten_query": self.rewritten_query,
            "sub_queries": self.sub_queries or None,
            "is_on_topic": self.is_on_topic,
            "retrieved_chunk_ids": [str(r["chunk_id"]) for r in self.results] if decided else None,
            "citations": self._citations() if decided else None,
            "prompt_version": self.version,
            "model": self.model,
            "usage_tokens": self.usage_tokens,
            "latency_ms": round((time.perf_counter() - self.started) * 1000),
        }
        message_id = await self.store.save_answer(str(self.thread["id"]), answer)
        if message_id is None:
            log.info("chat_thread_gone", extra={"thread_id": str(self.thread["id"]), "outcome": outcome})
            return None
        log.info(
            "chat_turn",
            extra={
                "thread_id": str(self.thread["id"]),
                "outcome": outcome,
                "prompt_version": self.version,
                "sub_queries": len(self.sub_queries),
                "retrieved": len(self.results),
                "top_score": self.top_score,
                "cited": len(self.rewriter.cited if self.rewriter else []),
                "usage_tokens": self.usage_tokens,
                "latency_ms": answer["latency_ms"],
                **self.timings,
            },
        )
        return message_id

    def _save_detached(self, outcome: str) -> asyncio.Task[str | None]:
        """Save in a task of its own, which a cancelled request does not cancel."""
        task = asyncio.get_running_loop().create_task(self._save(outcome))
        self.pending.add(task)  # a strong reference until it finishes; the lifespan waits for these
        task.add_done_callback(self.pending.discard)
        task.add_done_callback(_log_failure)
        return task


def topics(value: Any, query: str) -> list[str]:
    """The rewrite's sub_queries, cleaned: distinct, not the whole query, at most MAX_SUB_QUERIES. A single topic
    is no split, so it gives none."""
    if not isinstance(value, list):
        return []
    seen = {query.casefold()}
    out: list[str] = []
    for item in value:
        text = str(item).strip()[:MAX_MESSAGE]
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    out = out[:MAX_SUB_QUERIES]
    return out if len(out) > 1 else []


def merge(
    searches: list[list[dict[str, Any]]], floor: float, topic_floor: float
) -> list[tuple[dict[str, Any], int]]:
    """One list from several searches, each best first, as (result, the index of the search that found it): they
    take turns giving their next passage, so each topic gets its share. The whole message's search (the first)
    gives none if its best passage is under `floor`, a topic's search if under `topic_floor`: it found nothing on
    its topic. A passage already taken, or from a document that already has PER_DOCUMENT, is skipped. At most
    MERGED_TOP_K."""
    queues = [
        (n, list(rs))
        for n, rs in enumerate(searches)
        if rs and max(float(r["score"]) for r in rs) >= (floor if n == 0 else topic_floor)
    ]
    taken: set[str] = set()
    per_document: dict[str, int] = {}
    out: list[tuple[dict[str, Any], int]] = []
    while queues and len(out) < MERGED_TOP_K:
        for n, queue in queues:
            while queue:
                r = queue.pop(0)
                chunk, document = str(r["chunk_id"]), str(r.get("document_id"))
                if chunk in taken or per_document.get(document, 0) >= PER_DOCUMENT:
                    continue
                taken.add(chunk)
                per_document[document] = per_document.get(document, 0) + 1
                out.append((r, n))
                break
            if len(out) == MERGED_TOP_K:
                break
        queues = [(n, q) for n, q in queues if q]
    return out


def _log_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("answer_save_failed", exc_info=task.exception())


def needs_rewrite(content: str, history: list[dict[str, str]]) -> bool:
    # Skip only explicit, short, single-subject definitions on a first turn. Ambiguity keeps the model route.
    if history or not re.match(r"^(what (?:is|are)|define|explain)\s+", content.strip(), re.I):
        return True
    words = re.findall(r"[a-z0-9]+", content.lower())
    ambiguous = {
        "it",
        "that",
        "this",
        "those",
        "these",
        "they",
        "their",
        "and",
        "or",
        "versus",
        "vs",
        "compare",
        "apply",
        "relate",
        "ignore",
        "instructions",
        "system",
        "prompt",
    }
    return len(words) > 16 or bool(ambiguous.intersection(words)) or content.count("?") > 1
