"""the bounded tool-using turn loop both agents share (adrs 0014, 0021): each model turn either picks read-only tools or reports, the last turn must report, and a tool turn that cannot be used goes straight to the report. each agent brings its own workspace, tools, prompts and checks."""

import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from fastapi import HTTPException, Request
from learning_transport import Claims, UpstreamError, failure
from learning_transport.agents import MIN_CALL, OUT_OF_TIME, ai_core_refusal, unusable_reply

import checks
import prompts
from clients import GENERATE_TIMEOUT, Services, billed
from config import Settings
from contracts import Step
from upstream import TopicName
from workspace import Topic

Outcome = Literal["ok", "refused", "failed"]
# a tool call: the tool, the reference it reads (a result, student or question) and an optional topic.
Key = tuple[str, str | None, str | None]
TURN_TOKENS = 1500
REPORT_TOKENS = 3000
# time kept back for the report: a tool turn and its tools end this long before the budget runs out. it covers
# the report call's own timeout, so a late tool turn never leaves the report too little time.
REPORT_RESERVE = GENERATE_TIMEOUT + MIN_CALL
# a tool turn that fails with one of these goes straight to the report, which can still use what was gathered.
RECOVERABLE = {OUT_OF_TIME, "AI_TIMEOUT", "AI_OUTPUT_REJECTED", "AI_PROVIDER_FAILED"}


@dataclass(frozen=True)
class Wording:
    """what the teacher is told when the work took too long, access went, or a service is down."""

    slow: str
    no_access: str
    unavailable: str


def refused(e: UpstreamError, wording: Wording) -> HTTPException:
    """a failed upstream call as an error the page can act on, with a safe message."""
    if (known := ai_core_refusal(e)) is not None:
        return known
    if e.code == OUT_OF_TIME:
        return failure(503, OUT_OF_TIME, wording.slow)
    if e.service != "ai-core" and e.status in (401, 403):
        return failure(403, "FORBIDDEN", wording.no_access)
    if e.service != "ai-core" or e.status in (0, 503):
        return failure(503, "AI_UNAVAILABLE", wording.unavailable)
    return failure(502, "AI_PROVIDER_FAILED", "The model could not answer. Try again.")


class Topics(Protocol):
    """what the loop needs of an agent's workspace: its topics by reference, and how prerequisites are shown."""

    def topic_by_ref(self, ref: object) -> Topic | None: ...

    def prerequisites(self, topic: Topic, prerequisites: list[TopicName]) -> dict[str, Any]: ...


class ToolError(Exception):
    """a tool call the agent refused or could not complete, and what the model is told."""

    def __init__(self, target: str, outcome: Outcome, message: str) -> None:
        super().__init__(message)
        self.target, self.outcome, self.message = target, outcome, message


class Loop[R]:
    """the turn loop; a subclass names its log events, wording, schemas and tools, and checks the report."""

    log: logging.Logger
    event: str  # the prefix of the loop's log events, e.g. "interpretation"
    wording: Wording
    names: tuple[str, str]  # the schema names of a tool turn and of the report
    workspace: Topics
    reference = "result"  # the name of a call's first reference in the call and in the history

    def __init__(self, services: Services, settings: Settings, request: Request, claims: Claims) -> None:
        self.services, self.settings, self.request, self.claims = services, settings, request, claims
        self.authorization = request.headers["authorization"]
        self.correlation: str = request.state.correlation_id
        self.steps: list[Step] = []
        self.history: list[dict[str, Any]] = []
        self.done: set[Key] = set()  # calls that answered, not to repeat
        self.unreadable = 0
        self.failed_turns = 0
        self.started = time.monotonic()
        self.until = self.started + settings.budget  # no call runs past this
        self.reads: dict[str, asyncio.Future[Any]] = {}  # shared reads by key, while usable
        self.shown: set[str] = set()  # evidence refs whose answers the latest turn's task still held

    # ---- what a subclass provides

    def messages(self, turn: int, turns: int, final: bool) -> list[dict[str, str]]:
        raise NotImplementedError

    def schema(self, final: bool) -> dict[str, Any]:
        raise NotImplementedError

    def finish(self, report: dict[str, Any], turn: int) -> R:
        """the checked report as the response; may raise checks.unreadableoutputerror."""
        raise NotImplementedError

    async def tool(self, tool: str, ref: str | None, topic: Topic | None) -> tuple[str, Outcome, Any]:
        """what the tool looked at in words, its outcome, and what the model is shown; raises toolerror."""
        raise NotImplementedError

    # ---- the loop

    def refused(self, e: UpstreamError) -> HTTPException:
        return refused(e, self.wording)

    def words(self, ref: str) -> str | None:
        """a reference the model wrote, as the teacher reads it; none when the workspace never gave it out."""
        topic = self.workspace.topic_by_ref(ref)
        return topic.label if topic else None

    def shown_steps(self) -> list[Step]:
        """the steps with each reason's references in words, since the teacher never sees references."""

        def worded(m: re.Match[str]) -> str:
            return self.words(m[0]) or m[0]

        return [s.model_copy(update={"reason": checks.REFERENCE.sub(worded, s.reason)}) for s in self.steps]

    def turn_drops(self, dropped: Counter[str]) -> Counter[str]:
        """the checks' drops with the tool turns that could not be used."""
        if self.unreadable:
            dropped["UNREADABLE_TURN"] += self.unreadable
        if self.failed_turns:
            dropped["FAILED_TURN"] += self.failed_turns
        return dropped

    async def prerequisites(self, topic: Topic) -> tuple[str, Outcome, Any]:
        """the prerequisites tool both agents share."""
        try:
            found = await self.services.prerequisites(self.authorization, topic.topic_id, self.correlation)
        except UpstreamError:
            raise ToolError(topic.label, "failed", "The curriculum could not be read just now.") from None
        return topic.label, "ok", self.workspace.prerequisites(topic, found)

    async def shared[T](self, key: str, read: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """one read per key that parallel tool calls share; a failed read is dropped so a later call reads again."""
        future = self.reads.get(key)
        if future is None:
            future = self.reads[key] = asyncio.ensure_future(read())
        try:
            # shielded: one call running out of time does not cancel the read the others share.
            return cast(T, await asyncio.shield(future))
        except UpstreamError:
            if self.reads.get(key) is future:
                del self.reads[key]
            raise

    async def bounded(self, work: Awaitable[R]) -> R:
        """`work` within the request's whole budget, authorization and opening reads included."""
        try:
            async with asyncio.timeout(max(0.0, self.until - time.monotonic())):
                return await work
        except TimeoutError:
            raise self.refused(UpstreamError("ai-core", 0, OUT_OF_TIME)) from None
        finally:
            # a shared read a timed-out tool left running does not outlive the request. a failed read leaves
            # `reads` only once a call has awaited it, so its exception is already retrieved.
            for future in self.reads.values():
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()  # retrieved, so a failure is not logged as unhandled

    async def loop(self) -> R:
        """tool turns until the model reports or a turn must; the report goes to finish()."""
        turns = self.settings.max_turns
        for turn in range(1, turns + 1):
            now = time.monotonic()
            final = (
                turn == turns
                or now - self.started > self.settings.deadline
                or self.until - REPORT_RESERVE - now < MIN_CALL
            )
            reply = await self._turn(turn, turns, final)
            if reply is None:
                # an unreadable or failed tool turn: asking again the same way would likely fail the same way.
                turns = turn + 1
                continue
            if final:
                return self._finish(reply, turn)
            if reply.get("action") == "report" and isinstance(reply.get("report"), dict):
                return self._finish(reply["report"], turn)
            calls = reply.get("tool_calls") if isinstance(reply.get("tool_calls"), list) else []
            if not calls:
                # nothing to look up and no report: the next turn must report.
                turns = turn + 1
                continue
            await self._run_tools(calls[: prompts.MAX_CALLS], self.until - REPORT_RESERVE)
        raise unusable_reply()

    def _finish(self, report: dict[str, Any], turn: int) -> R:
        try:
            return self.finish(report, turn)
        except checks.UnreadableOutputError:
            raise unusable_reply() from None

    async def _turn(self, turn: int, turns: int, final: bool) -> dict[str, Any] | None:
        """one model turn's reply, or none when a tool turn was unreadable or failed recoverably (the next turn reports)."""
        messages = self.messages(turn, turns, final)
        self.shown = prompts.shown(messages)
        try:
            output = await self.services.generate(
                messages,
                REPORT_TOKENS if final else TURN_TOKENS,
                self.schema(final),
                self.names[1] if final else self.names[0],
                self.correlation,
                billed(self.claims),
                # a tool turn leaves the report its reserve; the report may use what is left.
                self.until - (0 if final else REPORT_RESERVE),
            )
        except UpstreamError as e:
            self.log.warning(f"{self.event}_failed", extra={"error": str(e), "turn": turn})
            if not final and e.code in RECOVERABLE:
                self.failed_turns += 1
                return None
            raise self.refused(e) from None
        try:
            return checks.parse(output)
        except checks.UnreadableOutputError:
            self.unreadable += 1
            if final:
                raise unusable_reply() from None
            return None

    async def _run_tools(self, calls: list[Any], until: float) -> None:
        """the turn's calls side by side, each cut off at `until`; recorded in the order the model asked."""
        planned: list[tuple[Key, str, bool]] = []
        asked = set(self.done)  # a call made twice in one turn runs once
        for call in calls:
            if not isinstance(call, dict):
                continue
            tool, reason = str(call.get("tool") or ""), " ".join(str(call.get("reason") or "").split())[:200]
            key = (tool, _ref(call.get(self.reference)), _ref(call.get("topic")))
            planned.append((key, reason, key in asked))
            asked.add(key)
        outcomes = await asyncio.gather(*(self._timed(key, repeated, until) for key, _, repeated in planned))
        for (key, reason, _), (target, outcome, output) in zip(planned, outcomes, strict=True):
            # only an answered call is refused as a repeat; a failed or timed-out one may be tried again.
            if outcome == "ok":
                self.done.add(key)
            tool, ref, topic = key
            made = {"tool": tool, self.reference: ref, "topic": topic}
            self.history.append({"call": made, "output": output})
            self.steps.append(
                Step(tool=tool, reason=reason or "No reason given.", target=target, outcome=outcome)
            )

    async def _timed(self, key: Key, repeated: bool, until: float) -> tuple[str, Outcome, Any]:
        """one tool call, failed when it is still running at `until`."""
        try:
            return await asyncio.wait_for(self._tool(key, repeated), max(until - time.monotonic(), 0.01))
        except TimeoutError:
            self.log.info("tool_failed", extra={"tool": key[0], "reason": "out_of_time"})
            return "out of time", "failed", {"error": "There was no time left to read this."}

    async def _tool(self, key: Key, repeated: bool) -> tuple[str, Outcome, Any]:
        tool, ref, topic_ref = key
        try:
            topic = self.workspace.topic_by_ref(topic_ref) if topic_ref else None
            if topic_ref and topic is None:
                raise ToolError("unknown topic", "refused", f"There is no topic {topic_ref}.")
            if repeated:
                raise ToolError("repeat", "refused", "You already made this call; its result is above.")
            return await self.tool(tool, ref, topic)
        except ToolError as e:
            if e.outcome == "failed":
                self.log.info("tool_failed", extra={"tool": key[0]})
            return e.target, e.outcome, {"error": e.message}


def _ref(value: object) -> str | None:
    """a reference the model gave, or none when it is not a non-empty string."""
    return value if isinstance(value, str) and value else None
