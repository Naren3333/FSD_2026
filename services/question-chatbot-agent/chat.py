"""one chat turn, ported from fsd_test's assistant: rewrite the question, search the class materials with the user's own token, and stream a cited answer from ai-core."""

import asyncio
import html
import json
import logging
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi.responses import StreamingResponse
from learning_transport import UpstreamError, sse_event
from learning_transport.agents import AI_CORE_CODES
from starlette.types import Receive, Scope, Send

import prompts
import support
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
    "section_path",
    "page_start",
    "page_end",
    "kind",
    "source",
    "content",
    "image",
)
# the thread was deleted (its classroom or its user purged) while the answer was written: nothing more is
# saved.
THREAD_GONE = ("NOT_FOUND", "This conversation was deleted.")
MAX_DOCUMENTS_CHARS = 55000  # ai-core takes messages of up to 60000 characters
PASSAGE_OVERHEAD = 200  # characters each passage adds to the prompt besides its content: tag, title, pages
MAX_SUB_QUERIES = 3
MERGED_TOP_K = 12  # passages a split message is answered from (one search gives SEARCH_TOP_K)
PER_DOCUMENT = 5  # of those, at most this many from one document


class SSEResponse(StreamingResponse):
    """close streaming generators promptly and run final cleanup even before streaming starts."""

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
    """the `error` event for a failed upstream call: a code the browser can act on and a safe message."""
    if e.code == "AI_SPEND_LIMIT":
        return "AI_SPEND_LIMIT", "You've used today's AI allowance."  # the event carries resets_at itself
    if e.code in AI_CORE_CODES:
        return e.code, AI_CORE_CODES[e.code][1]
    if e.service == "content":
        if e.status in (401, 403):
            return "FORBIDDEN", "You no longer have access to this classroom's materials."
        return "SEARCH_UNAVAILABLE", "Searching the class materials failed. Try again later."
    if e.service == "keycloak" or e.status in (0, 503):
        return "AI_UNAVAILABLE", "The assistant is not available right now."
    return "AI_PROVIDER_FAILED", "The model could not answer. Try again."


@dataclass(frozen=True)
class TurnRequest:
    """what the user asked, in which thread, and on whose behalf."""

    thread: dict[str, Any]
    authorization: str  # the user's own bearer token, for content search
    correlation: str
    content: str
    history: list[dict[str, str]]
    billed_to: dict[str, str]  # whose daily ai allowance pays for this turn's model calls
    claim: datetime  # the user's turn this answer holds (store.claim_turn), ended when it is saved


class Turn:
    """one answer: runs the pipeline, yields sse frames and saves the assistant message."""

    def __init__(
        self,
        services: Services,
        store: Store,
        settings: Settings,
        pending: set[asyncio.Task[Any]],
        request: TurnRequest,
    ) -> None:
        self.services, self.store, self.settings, self.pending = services, store, settings, pending
        self.billed_to, self.claim = request.billed_to, request.claim
        self.thread, self.authorization, self.correlation = (
            request.thread,
            request.authorization,
            request.correlation,
        )
        self.content, self.history = request.content, request.history
        self.started = time.perf_counter()
        # the ms each step took, for the chat_turn log: where a slow turn spent its time.
        self.timings: dict[str, int] = {}
        self.parts: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.rewriter: CitationRewriter | None = None
        self.version: str | None = None
        self.model: str | None = None
        self.usage_tokens: int | None = None
        self.outcome = "answered"
        self.rewritten_query: str | None = None  # none when the rewrite failed
        self.sub_queries: list[str] = []  # the topics of a message about several, each searched on its own
        # chunk id → {primary, topics}: every topic search that found the passage, and the one it ranked best in.
        self.passage_topics: dict[str, dict[str, Any]] = {}
        self.uncovered: list[str] = []  # the topics no cited passage of their own covers
        self.is_on_topic: bool | None = None
        self.small_talk = False
        self.searched = False
        self.top_score: float | None = None
        self.answer_check: dict[str, Any] | None = None  # the support check's record (adr 0025)
        # the cited statements the support check runs on after `done`; empty when no check runs.
        self.to_check: list[tuple[str, list[str]]] = []
        self.latency_ms: int | None = None  # when the answer was saved
        self.begun = False  # whether events() has started; finish() saves the turn when it never did

    def finish(self) -> None:
        """saves the turn as `interrupted` if its events never started: the client left before the stream began."""
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
            # the client went away.
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
        # saved before the last events, so a disconnect while they are sent can't lose the row, and
        # shielded, so a disconnect during the save can't either: the save carries on and the lifespan waits
        # for it.
        if error is None:
            saved = await self._saved(self.outcome)
            if isinstance(saved, str):
                # started before `done` is sent and detached, so a client that leaves from here on still
                # gets the check saved; the lifespan waits for it.
                check = detached(self.pending, self._verify(saved)) if self.to_check else None
                yield sse_event("citations", {"citations": self._citations()})
                yield sse_event(
                    "done", {"message_id": saved, "outcome": self.outcome, "checking": bool(check)}
                )
                if check is not None and (notice := await asyncio.shield(check)):
                    yield sse_event("notice", {"message_id": saved, "text": notice})
                return
            error = saved
        elif await self._saved("error") == THREAD_GONE:
            error, resets_at = THREAD_GONE, None
        data: dict[str, Any] = {"code": error[0], "message": error[1]}
        if resets_at:
            data["resets_at"] = resets_at
        yield sse_event("error", data)

    async def _saved(self, outcome: str) -> str | tuple[str, str]:
        """the saved answer's id, or the `error` event's code and message when it could not be saved."""
        try:
            message_id = await asyncio.shield(self._save_detached(outcome))
        except Exception:  # logged by _log_failure
            return ("INTERNAL_ERROR", "The answer could not be saved.")
        return THREAD_GONE if message_id is None else message_id

    async def _answer(self) -> AsyncIterator[str]:
        """answer small talk directly; refuse unrelated or weak evidence without an answer call."""
        role = self.thread["role"]
        step = time.perf_counter()
        await self._rewrite()
        step = self._took("rewrite_ms", step)
        if not self.small_talk and self.is_on_topic is not False:
            await self._search()
            step = self._took("search_ms", step)
        prompt = self._prompt(role)
        if prompt is None:
            self.outcome = "no_results"
            reply = prompts.refusal(role)
            self.parts.append(reply)
            yield sse_event("token", {"text": reply})
            return
        # closed with the turn, so a disconnect ends the provider stream at once.
        async with aclosing(self._stream(*prompt, step)) as pieces:
            async for out in pieces:
                self.parts.append(out)
                yield sse_event("token", {"text": out})
        answer = "".join(self.parts)
        self._took("answer_ms", step)
        if notice := self._coverage_notice():
            self.parts.append(notice)
            yield sse_event("token", {"text": notice})
        # an uncited answer is noted at once; a cited one is checked after `done` (_verify).
        if notice := self._uncited_notice(answer):
            self.parts.append(notice)
            yield sse_event("token", {"text": notice})

    def _prompt(self, role: str) -> tuple[list[dict[str, str]], dict[str, str]] | None:
        """the answer call's messages and citation tags, or none when the materials cannot answer it."""
        if self.small_talk:
            self.version = prompts.SMALL_TALK
            # no documents, so any tag the model writes is dropped
            return prompts.build_small_talk_messages(role, self.history, self.content), {}
        if (
            self.is_on_topic is False
            or self.top_score is None
            or self.top_score < self.settings.relevance_floor
        ):
            self.passage_topics = {}  # no prompt, so no passage was given a topic in one
            return None
        fitting = self._fitting()
        if not fitting:
            return None
        messages, self.version, tag_map = prompts.build_messages(
            role, fitting, self.history, self.content, self.passage_topics, self.sub_queries
        )
        return messages, tag_map

    async def _stream(
        self, messages: list[dict[str, str]], tag_map: dict[str, str], step: float
    ) -> AsyncGenerator[str]:
        """the answer's text with citation tags rewritten, as it arrives."""
        rewriter = self.rewriter = CitationRewriter(tag_map)
        async for item in self.services.stream(
            messages, self.settings.answer_max_tokens, self.correlation, self.billed_to
        ):
            if "first_token_ms" not in self.timings:
                self._took("first_token_ms", step)
            if isinstance(item, Done):
                self.model, self.usage_tokens = item.model, item.usage_tokens
            elif out := rewriter.feed(item):
                yield out
        if out := rewriter.flush():
            yield out

    def _coverage_notice(self) -> str | None:
        """a note naming the message's topics that no citation covers."""
        if self.small_talk or self.rewriter is None:
            return None
        # structural coverage only: a citation's presence does not establish semantic entailment.
        self.uncovered = support.uncovered(self.sub_queries, self.passage_topics, self.rewriter.cited)
        if not self.uncovered:
            return None
        log.warning("chat_topic_citations_incomplete", extra={"topics": self.uncovered})
        return (
            "\n\nCitation coverage is incomplete for: " + "; ".join(map(support.label, self.uncovered)) + "."
        )

    def _uncited_notice(self, answer: str) -> str | None:
        """notes an uncited answer that states something, with no model call; for a cited one, sets up the support check."""
        if self.small_talk or self.rewriter is None:
            return None
        if self.rewriter.cited:
            self.to_check = self._checkable(answer)
            return None
        if not support.factual(answer):
            return None
        self.answer_check = {"status": "uncited"}
        log.warning("chat_answer_uncited", extra={"thread_id": str(self.thread["id"])})
        return support.UNCITED_NOTICE

    def _checkable(self, answer: str) -> list[tuple[str, list[str]]]:
        """the answer's cited statements, each with the cited chunks that were in the prompt."""
        assert self.rewriter is not None
        given = {str(r["chunk_id"]) for r in self.results}
        known = [
            (text, [c for c in refs if c in self.rewriter.tag_map.values() and c in given])
            for text, refs in support.statements(answer)
        ]
        return [(text, refs) for text, refs in known if refs]

    async def _verify(self, message_id: str) -> str | None:
        """after `done`: checks the saved answer's cited statements, saves the record and any notice on it, and returns the notice. a failed check saves its status and shows nothing."""
        step = time.perf_counter()
        thread_id = str(self.thread["id"])
        try:
            self.answer_check = await self._check(self.to_check)
            self._took("check_ms", step)
            unsupported = [
                r["statement"]
                for r in self.answer_check.get("statements", [])
                if r["verdict"] == "unsupported"
            ]
            notice = support.unsupported_notice(unsupported) if unsupported else None
            if unsupported:
                log.warning(
                    "chat_answer_unsupported", extra={"thread_id": thread_id, "statements": len(unsupported)}
                )
            if not await self.store.save_check(message_id, self.answer_check, notice):
                # the thread was deleted meanwhile (THREAD_GONE): nothing more is saved or shown.
                log.info("chat_thread_gone", extra={"thread_id": thread_id, "outcome": self.outcome})
                return None
        except Exception:
            log.exception("answer_check_save_failed", extra={"thread_id": thread_id})
            return None
        finally:
            self._log_turn(self.outcome)
        if notice:
            self.parts.append(notice)
        return notice

    async def _check(self, checked: list[tuple[str, list[str]]]) -> dict[str, Any]:
        """the support check's record (answer_check.v1) for the answer's cited statements."""
        assert self.rewriter is not None
        tag_of = {cid: tag for tag, cid in self.rewriter.tag_map.items()}
        content = {str(r["chunk_id"]): str(r.get("content") or "") for r in self.results}
        cited = dict.fromkeys(c for _, refs in checked for c in refs)
        try:
            found = await support.check(
                self.services,
                {tag_of[c]: content[c] for c in cited},
                [(text, [tag_of[c] for c in refs]) for text, refs in checked],
                self.correlation,
                self.billed_to,  # the same user's allowance as the answer (adr 0003)
            )
        except Exception as e:  # never fails the turn: the answer stands without a notice
            log.warning(
                "answer_check_failed",
                extra={
                    "thread_id": str(self.thread["id"]),
                    "error": getattr(e, "code", None) or type(e).__name__,
                },
            )
            return {"status": "failed", "prompt_version": prompts.ANSWER_CHECK}
        return support.record(checked, found, prompts.ANSWER_CHECK)

    def _took(self, name: str, since: float) -> float:
        """records the ms since `since` as `name` and returns now, the next step's start."""
        now = time.perf_counter()
        self.timings[name] = round((now - since) * 1000)
        return now

    async def _search(self) -> None:
        """searches the whole message and, when it spans topics, each topic, all at once, then merges what came back."""
        queries = [self.rewritten_query or self.content, *self.sub_queries]
        classroom = str(self.thread["classroom_id"])

        # every search finishes before a failure is raised, so none is left running after the turn.
        async def lookup(q: str) -> list[dict[str, Any]]:
            results, fallback = await self.services.search(self.authorization, classroom, q, self.correlation)
            if fallback and results:
                raise UpstreamError("content", 503, "RERANK_UNAVAILABLE")
            return results

        if self.settings.adaptive_search and len(queries) > 1:
            primary = await lookup(queries[0])

            async def topic(q: str) -> list[dict[str, Any]]:
                return _covered(primary, q, self.settings.relevance_floor) or await lookup(q)

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
            # the turn is gated on the best passage; a much weaker one is not offered as evidence.
            self.results = [r for r in searches[0] if float(r["score"]) >= self.settings.topic_floor]
            return
        self.results = merge(searches, self.settings.relevance_floor, self.settings.topic_floor)
        self.passage_topics = attribute(
            self.results, self.sub_queries, searches[1:], self.settings.topic_floor
        )

    def _fitting(self) -> list[dict[str, Any]]:
        """the results, best first, that fit in one prompt message."""
        kept: list[dict[str, Any]] = []
        size = 0

        def section_markup(result: dict[str, Any]) -> str:
            if result.get("source") in ("markdown", "docx"):
                return html.escape(str(result.get("section_path") or ""))
            return ""

        def context_chars(result: dict[str, Any]) -> int:
            return len(result.get("content") or "") + len(section_markup(result)) + PASSAGE_OVERHEAD

        # count with the offline tokenizer and reserve space for markup, history and the current question.
        budget = max(
            0,
            self.settings.context_tokens
            - sum(token_bound(m["content"]) for m in self.history)
            - token_bound(self.content)
            - 1000,
        )
        # give each requested topic a chance before spending the remaining budget on extra passages: its best
        # passage of its own (primary), or else its best one shared with another topic.
        priority: list[dict[str, Any]] = []
        represented: set[str] = set()

        def topics_of(r: dict[str, Any]) -> dict[str, Any]:
            return self.passage_topics.get(str(r["chunk_id"]), {})

        for t in self.sub_queries:
            free = [r for r in self.results if str(r["chunk_id"]) not in represented]
            mine = [r for r in free if topics_of(r).get("primary") == t]
            mine = mine or [r for r in free if t in topics_of(r).get("topics", [])]
            if mine:
                priority.append(mine[0])
                represented.add(str(mine[0]["chunk_id"]))
        candidates = priority + [r for r in self.results if str(r["chunk_id"]) not in represented]
        for r in candidates:
            content = r.get("content") or ""
            cost = (
                token_bound(content)
                + token_bound(str(r.get("title") or ""))
                + token_bound(section_markup(r))
                + PASSAGE_OVERHEAD
            )
            if (
                size + cost > budget
                or sum(context_chars(k) for k in kept) + context_chars(r) > MAX_DOCUMENTS_CHARS
            ):
                continue
            size += cost
            kept.append(r)
        kept_ids = {str(r["chunk_id"]) for r in kept}
        self.results = [r for r in self.results if str(r["chunk_id"]) in kept_ids]
        self.passage_topics = {cid: v for cid, v in self.passage_topics.items() if cid in kept_ids}
        return self.results

    async def _rewrite(self) -> None:
        """a standalone search query, the two flags and the topics (query_rewrite.v4)."""
        if self.settings.adaptive_rewrite and not needs_rewrite(self.content, self.history):
            self.rewritten_query = self.content
            self.is_on_topic = True  # retrieval, never this heuristic, decides whether there is evidence.
            return
        try:
            output = await self.services.generate(
                prompts.rewrite_messages(self.history, self.content),
                REWRITE_MAX_TOKENS,
                prompts.REWRITE_SCHEMA,
                prompts.QUERY_REWRITE,
                self.correlation,
                self.billed_to,
                profile="fast",  # a short rewrite: no reasoning (adr 0023)
            )
            out = json.loads(output)
            query, on_topic, small_talk = (
                str(out["query"]),
                bool(out["is_on_topic"]),
                bool(out["is_small_talk"]),
            )
        except UpstreamError as e:
            if error_of(e)[0] in (
                "AI_UNAVAILABLE",
                "AI_SPEND_LIMIT",
                "AI_BUSY",
                "AI_PROVIDER_NOT_CONFIGURED",
            ):
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
        """the cited chunks, in citation order."""
        if self.rewriter is None:
            return []
        by_id = {str(r["chunk_id"]): r for r in self.results}
        return [{k: by_id[cid].get(k) for k in CITATION_FIELDS} for cid in self.rewriter.cited]

    async def _save(self, outcome: str) -> str | None:
        try:
            answer, message_id = await self._store(outcome)
        except Exception:
            # nothing else ends the claim, so the user could not ask again until it went stale.
            await release(self.store, str(self.thread["id"]), self.claim)
            raise
        if message_id is None:
            log.info("chat_thread_gone", extra={"thread_id": str(self.thread["id"]), "outcome": outcome})
            return None
        self.latency_ms = answer["latency_ms"]
        if not self.to_check or outcome != self.outcome:
            self._log_turn(outcome)  # else once the support check ends, with its record and check_ms
        return message_id

    def _log_turn(self, outcome: str) -> None:
        """the chat_turn log line: what the turn did and where its time went."""
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
                # topics with a passage of their own in the prompt, and those none of theirs was cited for.
                "primary_topics": len({v["primary"] for v in self.passage_topics.values()}),
                "uncovered_topics": len(self.uncovered),
                "answer_check": self.answer_check and self.answer_check["status"],
                "unsupported": (self.answer_check or {}).get("counts", {}).get("unsupported"),
                "usage_tokens": self.usage_tokens,
                "latency_ms": self.latency_ms,
                **self.timings,
            },
        )

    async def _store(self, outcome: str) -> tuple[dict[str, Any], str | None]:
        """the answer as saved, and its id; none when the thread is gone."""
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
            "answer_check": self.answer_check,
            "passage_topics": saved_topics(self.passage_topics) if decided else None,
        }
        return answer, await self.store.save_answer(str(self.thread["id"]), answer, self.claim)

    def _save_detached(self, outcome: str) -> asyncio.Task[str | None]:
        """save in a task of its own, which a cancelled request does not cancel."""
        task = detached(self.pending, self._save(outcome))
        task.add_done_callback(_log_failure)
        return task


def detached[T](pending: set[asyncio.Task[Any]], work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """runs work in a task of its own, which a cancelled request does not cancel."""
    task = asyncio.get_running_loop().create_task(work)
    pending.add(task)  # a strong reference until it finishes; the lifespan waits for these
    task.add_done_callback(pending.discard)
    return task


async def release(store: Store, thread_id: str, claim: datetime) -> None:
    """gives up a turn's claim; a failure leaves it to go stale, without hiding the error that ended the turn."""
    try:
        await store.release_turn(thread_id, claim)
    except Exception:
        log.warning("chat_turn_release_failed", extra={"thread_id": thread_id})


def topics(value: Any, query: str) -> list[str]:
    """the rewrite's sub_queries, cleaned: distinct, not the whole query, at most MAX_SUB_QUERIES."""
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


_STOPWORDS = frozenset({"the", "a", "an", "of", "in", "to", "and", "for"})


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _named(topic: str, result: dict[str, Any]) -> int:
    """how many of the topic's words the passage's title and section name."""
    heading = _words(str(result.get("title") or "") + " " + str(result.get("section_path") or ""))
    return len((_words(topic) - _STOPWORDS) & heading)


def attribute(
    results: list[dict[str, Any]], sub_queries: list[str], legs: list[list[dict[str, Any]]], floor: float
) -> dict[str, dict[str, Any]]:
    """chunk id → {primary, topics} for the kept passages a topic search found at `floor` or above.

    the primary topic is the search the passage ranked best in, ties going to the topic more of whose words its
    title or section names, then to the earlier topic. a topic left with no primary passage takes its best-ranked
    passage from a topic that has another, so a passage shared by two topics can't leave one with none."""
    kept = {str(r["chunk_id"]): r for r in results}
    rank: dict[str, dict[str, int]] = {}
    for t, passages in zip(sub_queries, legs, strict=True):
        for n, passage in enumerate(passages):
            cid = str(passage["chunk_id"])
            if cid in kept and float(passage["score"]) >= floor:
                rank.setdefault(cid, {}).setdefault(t, n)

    def key(cid: str, t: str) -> tuple[int, int, int]:
        return rank[cid][t], -_named(t, kept[cid]), sub_queries.index(t)

    out: dict[str, dict[str, Any]] = {
        cid: {"primary": min(found, key=lambda t: key(cid, t)), "topics": list(found)}
        for cid, found in rank.items()
    }
    for t in sub_queries:
        owners = [v["primary"] for v in out.values()]
        if t in owners:
            continue
        spare = [cid for cid, v in out.items() if t in v["topics"] and owners.count(v["primary"]) > 1]
        if spare:
            out[min(spare, key=lambda cid: key(cid, t))]["primary"] = t
    return out


def saved_topics(passage_topics: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    """the passage topics as saved with the answer: chunk ids and topic labels only, never passage text."""
    return {
        cid: {"primary": support.label(v["primary"]), "topics": [support.label(t) for t in v["topics"]]}
        for cid, v in passage_topics.items()
    } or None


def _covered(primary: list[dict[str, Any]], query: str, floor: float) -> list[dict[str, Any]]:
    """the primary search's passages whose title and section name every word of `query` and score at least `floor`."""
    terms = _words(query) - _STOPWORDS
    if not terms:
        return []
    return [
        r
        for r in primary
        if terms.issubset(_words(str(r.get("title", "")) + " " + str(r.get("section_path", ""))))
        and float(r["score"]) >= floor
    ]


def merge(searches: list[list[dict[str, Any]]], floor: float, topic_floor: float) -> list[dict[str, Any]]:
    """one list from several searches, each best first: they take turns giving their next passage, so each topic gets its share. a passage below `topic_floor` is left out."""
    queues = [
        (n, list(rs))
        for n, rs in enumerate(searches)
        if rs and max(float(r["score"]) for r in rs) >= (floor if n == 0 else topic_floor)
    ]
    taken: set[str] = set()
    per_document: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    while queues and len(out) < MERGED_TOP_K:
        for _, queue in queues:
            while queue:
                r = queue.pop(0)
                chunk, document = str(r["chunk_id"]), str(r.get("document_id"))
                if (
                    chunk in taken
                    or float(r["score"]) < topic_floor
                    or per_document.get(document, 0) >= PER_DOCUMENT
                ):
                    continue
                taken.add(chunk)
                per_document[document] = per_document.get(document, 0) + 1
                out.append(r)
                break
            if len(out) == MERGED_TOP_K:
                break
        queues = [(n, q) for n, q in queues if q]
    return out


def _log_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("answer_save_failed", exc_info=task.exception())


def needs_rewrite(content: str, history: list[dict[str, str]]) -> bool:
    # skip only explicit, short, single-subject definitions on a first turn. a question that describes a
    # situation ("what is the problem when ...") may need its concept named (query_rewrite.v4), so it is
    # rewritten.
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
        "when",
        "why",
        "how",
        "if",
        "happens",
        "wrong",
    }
    return len(words) > 16 or bool(ambiguous.intersection(words)) or content.count("?") > 1
