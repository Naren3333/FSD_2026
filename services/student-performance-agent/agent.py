"""performance interpretation (adr 0014): a bounded tool-using loop. the agent opens with the student's topic summary and finalized results, then each model turn either picks read-only tools or reports; the last turn must report. the report is checked against what the tools returned."""

import asyncio
import logging
import time
from collections import Counter
from typing import Any, Literal

from fastapi import HTTPException, Request
from learning_transport import RELATION_TEACHER, Claims, UpstreamError, failure, require_relation
from learning_transport.agents import ai_core_refusal, unusable_reply

import checks
import prompts
from clients import Services, billed
from config import Settings
from contracts import Evidence, Observation, RequestContract, ResponseContract, Step
from workspace import MAX_RESULTS, Topic, Workspace

log = logging.getLogger("student-performance-agent.agent")

Outcome = Literal["ok", "refused", "failed"]
# a model call the budget stopped or kept from starting.
OUT_OF_TIME = "OUT_OF_TIME"
TURN_TOKENS = 1500
REPORT_TOKENS = 3000
# time kept back for the report: a tool turn and its tools end this long before the budget runs out.
REPORT_RESERVE = 60.0
# a call with less time than this left is not started.
MIN_CALL = 5.0
NOT_TEACHER = ("FORBIDDEN", "Only the classroom's teacher can interpret a student's results.")


def refused(e: UpstreamError) -> HTTPException:
    """a failed upstream call as an error the page can act on, with a safe message."""
    if (known := ai_core_refusal(e)) is not None:
        return known
    if e.code == OUT_OF_TIME:
        return failure(
            503, OUT_OF_TIME, "Interpreting these results took too long. Try again, or try again later."
        )
    if e.service != "ai-core" and e.status in (401, 403):
        return failure(403, "FORBIDDEN", "You no longer have access to this student's results.")
    if e.service != "ai-core" or e.status in (0, 503):
        return failure(
            503, "AI_UNAVAILABLE", "Interpreting results is not available right now. Try again later."
        )
    return failure(502, "AI_PROVIDER_FAILED", "The model could not answer. Try again.")


class ToolError(Exception):
    """a tool call the agent refused or could not complete, and what the model is told."""

    def __init__(self, target: str, outcome: Outcome, message: str) -> None:
        super().__init__(message)
        self.target, self.outcome, self.message = target, outcome, message


class Agent:
    def __init__(self, services: Services, settings: Settings, request: Request, claims: Claims) -> None:
        self.services, self.settings, self.request, self.claims = services, settings, request, claims
        self.authorization = request.headers["authorization"]
        self.correlation: str = request.state.correlation_id
        self.workspace = Workspace()
        self.student = ""  # set by interpret
        self.steps: list[Step] = []
        self.history: list[dict[str, Any]] = []
        self.done: set[tuple[str, str | None, str | None]] = set()  # calls that answered, not to repeat
        self.unreadable = 0
        self.started = time.monotonic()
        self.until = self.started + settings.budget  # no call runs past this
        self.class_read: asyncio.Future[list[Any]] | None = None  # the class's summaries, read once

    async def interpret(self, body: RequestContract) -> ResponseContract:
        classroom, student = str(body.classroom_id), body.student_id
        self.student = student
        # the caller must teach the classroom and the student must be enrolled in it.
        await require_relation(
            self.services.identity,
            self.request,
            classroom,
            student_id=student,
            allowed=(RELATION_TEACHER,),
            denied=NOT_TEACHER,
        )
        try:
            await self._open(classroom, student)
        except UpstreamError as e:
            log.warning("interpretation_open_failed", extra={"error": str(e)})
            raise refused(e) from None
        if not self.workspace.results:
            raise failure(
                422,
                "NO_EVIDENCE",
                "This student has no finalized results in this classroom yet, so there is nothing to interpret.",
            )
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
                continue
            if final:
                return self._finish(reply, turn, classroom, student)
            if reply.get("action") == "report" and isinstance(reply.get("report"), dict):
                return self._finish(reply["report"], turn, classroom, student)
            calls = reply.get("tool_calls") if isinstance(reply.get("tool_calls"), list) else []
            if not calls:
                # nothing to look up and no report: the next turn must report.
                turns = turn + 1
                continue
            await self._run_tools(classroom, calls[: prompts.MAX_CALLS], self.until - REPORT_RESERVE)
        raise unusable_reply()

    async def _open(self, classroom: str, student: str) -> None:
        """the student's released topic summary and finalized results, and the names of their topics."""
        summary, evaluations = await asyncio.gather(
            self.services.performance(self.authorization, classroom, student, self.correlation),
            self.services.results(self.authorization, classroom, student, self.correlation, MAX_RESULTS),
        )
        # only the newest results reach the workspace, so only their assessments are read.
        try:
            self.workspace.add_summary(list(summary.get("topics") or []))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UpstreamError("analytics", 502) from None
        # every topic is in the summary or the evidence, so names are read alongside the assessments.
        topics = list(self.workspace.topics) + [
            e["topic_id"]
            for v in evaluations
            for e in v.get("evidence") or []
            if isinstance(e, dict) and isinstance(e.get("topic_id"), str)
        ]
        assessments, names = await asyncio.gather(
            self.services.assessments(
                self.authorization,
                list(dict.fromkeys(e["assessment_id"] for e in evaluations)),
                self.correlation,
            ),
            self.services.topic_names(self.authorization, list(dict.fromkeys(topics)), self.correlation),
        )
        try:
            self.workspace.add_results(evaluations, assessments)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UpstreamError("grading", 502) from None
        self.workspace.name_topics(names)
        self.workspace.fit()

    async def _turn(self, turn: int, turns: int, final: bool) -> dict[str, Any] | None:
        """one model turn's reply, or none when it was unreadable (the loop moves on)."""
        messages = prompts.messages(self.workspace.opening(), self.history, turn, turns, final)
        # a tool turn leaves the report its reserve; the report may use what is left.
        left = self.until - time.monotonic() - (0 if final else REPORT_RESERVE)
        try:
            if left < MIN_CALL:
                raise UpstreamError("ai-core", 0, OUT_OF_TIME)
            output = await self.services.generate(
                messages,
                REPORT_TOKENS if final else TURN_TOKENS,
                prompts.schema(final),
                "performance.report" if final else "performance.turn",
                self.correlation,
                billed(self.claims),
                left,
            )
        except UpstreamError as e:
            log.warning("interpretation_failed", extra={"error": str(e), "turn": turn})
            # an unanswered call that ran until the budget was cut off by it, not by ai-core.
            if e.status == 0 and time.monotonic() >= self.until - MIN_CALL:
                e = UpstreamError("ai-core", 0, OUT_OF_TIME)
            raise refused(e) from None
        try:
            return checks.parse(output)
        except checks.UnreadableOutputError:
            self.unreadable += 1
            if final:
                raise unusable_reply() from None
            return None

    async def _run_tools(self, classroom: str, calls: list[Any], until: float) -> None:
        """the turn's calls side by side, each cut off at `until`; recorded in the order the model asked."""
        planned = []
        asked = set(self.done)  # a call made twice in one turn runs once
        for call in calls:
            if not isinstance(call, dict):
                continue
            tool, reason = str(call.get("tool") or ""), " ".join(str(call.get("reason") or "").split())[:200]
            result_ref, topic_ref = _ref(call.get("result")), _ref(call.get("topic"))
            key = (tool, result_ref, topic_ref)
            planned.append((key, reason, key in asked))
            asked.add(key)
        outcomes = await asyncio.gather(
            *(self._timed(classroom, *key, repeat, until) for key, _, repeat in planned)
        )
        for ((tool, result_ref, topic_ref), reason, _), (target, outcome, output) in zip(
            planned, outcomes, strict=True
        ):
            # only an answered call is refused as a repeat; a failed or timed-out one may be tried again.
            if outcome == "ok":
                self.done.add((tool, result_ref, topic_ref))
            made = {"tool": tool, "result": result_ref, "topic": topic_ref}
            self.history.append({"call": made, "output": output})
            self.steps.append(
                Step(tool=tool, reason=reason or "No reason given.", target=target, outcome=outcome)
            )

    async def _timed(
        self,
        classroom: str,
        tool: str,
        result_ref: str | None,
        topic_ref: str | None,
        repeat: bool,
        until: float,
    ) -> tuple[str, Outcome, Any]:
        """one tool call, failed when it is still running at `until`."""
        try:
            return await asyncio.wait_for(
                self._tool(classroom, tool, result_ref, topic_ref, repeat),
                max(until - time.monotonic(), 0.01),
            )
        except TimeoutError:
            log.info("tool_failed", extra={"tool": tool, "reason": "out_of_time"})
            return "out of time", "failed", {"error": "There was no time left to read this."}

    async def _tool(
        self, classroom: str, tool: str, result_ref: str | None, topic_ref: str | None, repeat: bool
    ) -> tuple[str, Outcome, Any]:
        """what the tool looked at in words, its outcome, and what the model is shown."""
        try:
            topic = self.workspace.topic_by_ref(topic_ref) if topic_ref else None
            if topic_ref and topic is None:
                raise ToolError("unknown topic", "refused", f"There is no topic {topic_ref}.")
            if repeat:
                raise ToolError("repeat", "refused", "You already made this call; its result is above.")
            if tool == "read_answers":
                return self._read_answers(result_ref, topic)
            if tool == "prerequisites":
                return await self._prerequisites(topic)
            if tool == "class_comparison":
                return await self._class_comparison(classroom, topic)
            raise ToolError("unknown tool", "refused", f"There is no tool {tool}.")
        except ToolError as e:
            if e.outcome == "failed":
                log.info("tool_failed", extra={"tool": tool})
            return e.target, e.outcome, {"error": e.message}

    def _read_answers(self, result_ref: str | None, topic: Topic | None) -> tuple[str, Outcome, Any]:
        result = self.workspace.result(result_ref)
        if result is None:
            raise ToolError("unknown result", "refused", "Give a result reference from the results list.")
        target = result.title + (f", {topic.name}" if topic and topic.name else "")
        return target, "ok", self.workspace.read_answers(result, topic)

    async def _prerequisites(self, topic: Topic | None) -> tuple[str, Outcome, Any]:
        if topic is None:
            raise ToolError("no topic", "refused", "Give the topic whose prerequisites you want.")
        target = topic.name or "a topic"
        try:
            found = await self.services.prerequisites(self.authorization, topic.topic_id, self.correlation)
        except UpstreamError:
            raise ToolError(target, "failed", "The curriculum could not be read just now.") from None
        return target, "ok", self.workspace.prerequisites(topic, found)

    async def _class_comparison(self, classroom: str, topic: Topic | None) -> tuple[str, Outcome, Any]:
        target = topic.name if topic and topic.name else "whole class"
        if self.class_read is None:
            self.class_read = asyncio.ensure_future(
                self.services.class_performance(self.authorization, classroom, self.correlation)
            )
        unread = ToolError(target, "failed", "The class's results could not be read just now.")
        try:
            # shielded: one call running out of time does not cancel the read the others share.
            students = await asyncio.shield(self.class_read)
        except UpstreamError:
            self.class_read = None  # a later call reads the class again
            raise unread from None
        try:
            return target, "ok", self.workspace.class_comparison(students, topic, self.student)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise unread from None

    def _finish(self, report: dict[str, Any], turn: int, classroom: str, student: str) -> ResponseContract:
        try:
            checked = checks.report(report, self.workspace)
        except checks.UnreadableOutputError:
            raise unusable_reply() from None
        except checks.RejectedError:
            log.info("interpretation_rejected", extra={"turns": turn})
            raise failure(
                422,
                "INTERPRETATION_REJECTED",
                "The drafted interpretation did not rest on this student's evidence, so it was not shown. Try again.",
            ) from None
        dropped: Counter[str] = checked.dropped
        if self.unreadable:
            dropped["UNREADABLE_TURN"] += self.unreadable
        cited = {q.ref: q for f in checked.observations + checked.misconceptions for q in f.evidence}
        log.info(
            "interpreted",
            extra={"turns": turn, "tools": len(self.steps), "dropped": dict(dropped), "cited": len(cited)},
        )
        return ResponseContract(
            classroom_id=classroom,  # type: ignore[arg-type]
            student_id=student,
            observations=[_observation(f) for f in checked.observations],
            possible_misconceptions=[_observation(f) for f in checked.misconceptions],
            suggested_interventions=checked.interventions,
            recommended_topic_ids=checked.topic_ids,  # type: ignore[arg-type]
            evidence=[
                Evidence(
                    question_id=q.question_id,  # type: ignore[arg-type]
                    evaluation_id=q.evaluation_id,  # type: ignore[arg-type]
                    assessment_id=q.assessment_id,  # type: ignore[arg-type]
                    assessment_title=q.assessment_title,
                    topic_id=q.topic_id,  # type: ignore[arg-type]
                    prompt=str(q.question.get("prompt") or ""),
                    awarded=q.awarded,
                    possible=q.possible,
                )
                for q in cited.values()
            ],
            steps=self.steps,
            model_turns=turn,
            dropped=dict(dropped),
        )


def _ref(value: object) -> str | None:
    """a reference the model gave, or none when it is not a non-empty string."""
    return value if isinstance(value, str) and value else None


def _observation(f: checks.Finding) -> Observation:
    return Observation(text=f.text, evidence_ids=[q.question_id for q in f.evidence])  # type: ignore[misc]
