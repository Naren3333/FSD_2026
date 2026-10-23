"""performance interpretation (adr 0014): a bounded tool-using loop. the agent opens with the student's topic summary and finalized results, then each model turn either picks read-only tools or reports; the last turn must report. the report is checked against what the tools returned."""

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException, Request
from learning_transport import RELATION_TEACHER, Claims, UpstreamError, failure, require_relation

import checks
import prompts
from clients import Services, billed
from config import Settings
from contracts import Evidence, Observation, RequestContract, ResponseContract, Step
from workspace import Topic, Workspace

log = logging.getLogger("student-performance-agent.agent")

Outcome = Literal["ok", "refused", "failed"]
TURN_TOKENS = 1500
REPORT_TOKENS = 3000
NOT_TEACHER = ("FORBIDDEN", "Only the classroom's teacher can interpret a student's results.")

# errors whose code alone says what the teacher should hear.
BY_CODE = {
    "AI_BUSY": (429, "You have too many AI requests running at once. Try again in a moment."),
    "AI_PROVIDER_NOT_CONFIGURED": (503, "AI is not set up on this server."),
    "AI_OUTPUT_REJECTED": (502, "The model did not return a usable reply. Try again."),
}


def refused(e: UpstreamError) -> HTTPException:
    """a failed upstream call as an error the page can act on, with a safe message."""
    if e.code == "AI_SPEND_LIMIT":
        when = _local_time(e.resets_at)
        message = "You've used today's AI allowance." + (f" It resets at {when}." if when else "")
        return failure(429, "AI_SPEND_LIMIT", message)
    if e.code in BY_CODE:
        return failure(BY_CODE[e.code][0], e.code, BY_CODE[e.code][1])
    if e.service != "ai-core" and e.status in (401, 403):
        return failure(403, "FORBIDDEN", "You no longer have access to this student's results.")
    if e.service != "ai-core" or e.status in (0, 503):
        return failure(
            503, "AI_UNAVAILABLE", "Interpreting results is not available right now. Try again later."
        )
    return failure(502, "AI_PROVIDER_FAILED", "The model could not answer. Try again.")


def _local_time(iso: str | None) -> str:
    """ai-core's reset time in its own zone, with the zone's offset, e.g. \"00:00 on 19 oct (utc+08:00)\"."""
    try:
        at = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return ""
    if at is None:
        return ""
    zone = at.strftime(" (%Z)") if at.tzinfo is not None else ""
    return at.strftime("%H:%M on %d %b") + zone


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
        self.steps: list[Step] = []
        self.history: list[dict[str, Any]] = []
        self.done: set[tuple[str, str | None, str | None]] = set()
        self.unreadable = 0

    async def interpret(self, body: RequestContract) -> ResponseContract:
        classroom, student = str(body.classroom_id), body.student_id
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
        started = time.monotonic()
        turns = self.settings.max_turns
        for turn in range(1, turns + 1):
            final = turn == turns or time.monotonic() - started > self.settings.deadline
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
            await self._run_tools(classroom, calls[: prompts.MAX_CALLS])
        raise failure(502, "AI_OUTPUT_REJECTED", "The model did not return a usable reply. Try again.")

    async def _open(self, classroom: str, student: str) -> None:
        """the student's released topic summary and finalized results, and the names of their topics."""
        summary, evaluations = await asyncio.gather(
            self.services.performance(self.authorization, classroom, student, self.correlation),
            self.services.results(self.authorization, classroom, student, self.correlation),
        )
        assessments = await self.services.assessments(
            self.authorization, list(dict.fromkeys(e["assessment_id"] for e in evaluations)), self.correlation
        )
        try:
            self.workspace.add_summary(list(summary.get("topics") or []))
            self.workspace.add_results(evaluations, assessments)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UpstreamError("grading", 502) from None
        names = await self.services.topic_names(
            self.authorization, list(self.workspace.topics), self.correlation
        )
        self.workspace.name_topics(names)

    async def _turn(self, turn: int, turns: int, final: bool) -> dict[str, Any] | None:
        """one model turn's reply, or none when it was unreadable (the loop moves on)."""
        messages = prompts.messages(self.workspace.opening(), self.history, turn, turns, final)
        try:
            output = await self.services.generate(
                messages,
                REPORT_TOKENS if final else TURN_TOKENS,
                prompts.schema(final),
                "performance.report" if final else "performance.turn",
                self.correlation,
                billed(self.claims),
            )
        except UpstreamError as e:
            log.warning("interpretation_failed", extra={"error": str(e), "turn": turn})
            raise refused(e) from None
        try:
            return checks.parse(output)
        except checks.UnreadableOutputError:
            self.unreadable += 1
            if final:
                raise failure(
                    502, "AI_OUTPUT_REJECTED", "The model did not return a usable reply. Try again."
                ) from None
            return None

    async def _run_tools(self, classroom: str, calls: list[Any]) -> None:
        for call in calls:
            if not isinstance(call, dict):
                continue
            tool, reason = str(call.get("tool") or ""), " ".join(str(call.get("reason") or "").split())[:200]
            result_ref, topic_ref = call.get("result"), call.get("topic")
            key = (tool, result_ref, topic_ref)
            entry: dict[str, Any] = {"call": {"tool": tool, "result": result_ref, "topic": topic_ref}}
            target, outcome, output = await self._tool(classroom, tool, result_ref, topic_ref, key)
            self.done.add(key)
            entry["output"] = output
            self.history.append(entry)
            self.steps.append(
                Step(tool=tool, reason=reason or "No reason given.", target=target, outcome=outcome)
            )

    async def _tool(
        self, classroom: str, tool: str, result_ref: object, topic_ref: object, key: tuple[Any, ...]
    ) -> tuple[str, Outcome, Any]:
        """what the tool looked at in words, its outcome, and what the model is shown."""
        try:
            topic = self.workspace.topic_by_ref(topic_ref) if topic_ref else None
            if topic_ref and topic is None:
                raise ToolError("unknown topic", "refused", f"There is no topic {topic_ref}.")
            if key in self.done:
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

    def _read_answers(self, result_ref: object, topic: Topic | None) -> tuple[str, Outcome, Any]:
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
        try:
            students = await self.services.class_performance(self.authorization, classroom, self.correlation)
        except UpstreamError:
            raise ToolError(target, "failed", "The class's results could not be read just now.") from None
        return target, "ok", self.workspace.class_comparison(students, topic)

    def _finish(self, report: dict[str, Any], turn: int, classroom: str, student: str) -> ResponseContract:
        try:
            checked = checks.report(report, self.workspace)
        except checks.UnreadableOutputError:
            raise failure(
                502, "AI_OUTPUT_REJECTED", "The model did not return a usable reply. Try again."
            ) from None
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


def _observation(f: checks.Finding) -> Observation:
    return Observation(text=f.text, evidence_ids=[q.question_id for q in f.evidence])  # type: ignore[misc]
