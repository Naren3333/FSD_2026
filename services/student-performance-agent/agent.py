"""performance interpretation (adr 0014): a bounded tool-using loop. the agent opens with the student's topic summary and finalized results, then each model turn either picks read-only tools or reports; the last turn must report. the report is checked against what the tools returned."""

import logging
from typing import Any

from fastapi import Request
from learning_transport import RELATION_TEACHER, Claims, UpstreamError, failure, require_relation
from learning_transport.agents import gather_all

import checks
import prompts
from clients import Services
from config import Settings
from contracts import ClassComparisonScope, Evidence, Observation, RequestContract, ResponseContract
from loop import Loop, Outcome, ToolError, Wording
from workspace import MAX_RESULTS, Topic, Workspace, evidence_fields

log = logging.getLogger("student-performance-agent.agent")

NOT_TEACHER = ("FORBIDDEN", "Only the classroom's teacher can interpret a student's results.")
WORDING = Wording(
    slow="Interpreting these results took too long. Try again, or try again later.",
    no_access="You no longer have access to this student's results.",
    unavailable="Interpreting results is not available right now. Try again later.",
)


class Agent(Loop[ResponseContract]):
    log = log
    event = "interpretation"
    wording = WORDING
    names = ("performance.turn", "performance.report")
    workspace: Workspace

    def __init__(self, services: Services, settings: Settings, request: Request, claims: Claims) -> None:
        super().__init__(services, settings, request, claims)
        self.workspace = Workspace()
        self.classroom = self.student = ""  # set by interpret
        self.comparison_scope: ClassComparisonScope | None = None

    async def interpret(self, body: RequestContract) -> ResponseContract:
        return await self.bounded(self._interpret(body))

    async def _interpret(self, body: RequestContract) -> ResponseContract:
        classroom, student = str(body.classroom_id), body.student_id
        self.classroom, self.student = classroom, student
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
            raise self.refused(e) from None
        if not self.workspace.results:
            raise failure(
                422,
                "NO_EVIDENCE",
                "This student has no finalized results in this classroom yet, so there is nothing to interpret.",
            )
        return await self.loop()

    async def _open(self, classroom: str, student: str) -> None:
        """the student's released topic summary and finalized results, and the names of their topics."""
        summary, sample = await gather_all(
            self.services.performance(self.authorization, classroom, student, self.correlation),
            self.services.results(self.authorization, classroom, student, self.correlation, MAX_RESULTS),
        )
        evaluations = sample.evaluations
        # only the newest results reach the workspace, so only their assessments are read.
        self.workspace.add_summary(summary["topics"])
        # every topic is in the summary or the evidence, so names are read alongside the assessments.
        topics = list(self.workspace.topics) + [e["topic_id"] for v in evaluations for e in v["evidence"]]
        assessments, names = await gather_all(
            self.services.assessments(
                self.authorization,
                list(dict.fromkeys(e["assessment_id"] for e in evaluations)),
                self.correlation,
            ),
            self.services.topic_names(self.authorization, list(dict.fromkeys(topics)), self.correlation),
        )
        self.workspace.add_results(evaluations, assessments)
        self.workspace.name_topics(names)
        self.workspace.fit()

    def messages(self, turn: int, turns: int, final: bool) -> list[dict[str, str]]:
        return prompts.messages(self.workspace.opening(), self.history, turn, turns, final)

    def schema(self, final: bool) -> dict[str, Any]:
        return prompts.schema(
            final, list(self.workspace.questions()), [t.ref for t in self.workspace.topics.values()]
        )

    async def tool(self, tool: str, result_ref: str | None, topic: Topic | None) -> tuple[str, Outcome, Any]:
        if tool == "read_answers":
            return self._read_answers(result_ref, topic)
        if tool == "prerequisites":
            if topic is None:
                raise ToolError("no topic", "refused", "Give the topic whose prerequisites you want.")
            return await self.prerequisites(topic)
        if tool == "class_comparison":
            return await self._class_comparison(topic)
        raise ToolError("unknown tool", "refused", f"There is no tool {tool}.")

    def _read_answers(self, result_ref: str | None, topic: Topic | None) -> tuple[str, Outcome, Any]:
        result = self.workspace.result(result_ref)
        if result is None:
            raise ToolError("unknown result", "refused", "Give a result reference from the results list.")
        target = result.title + (f", {topic.name}" if topic and topic.name else "")
        return target, "ok", self.workspace.read_answers(result, topic)

    async def _class_comparison(self, topic: Topic | None) -> tuple[str, Outcome, Any]:
        target = topic.name if topic and topic.name else "whole class"
        unread = ToolError(target, "failed", "The class's results could not be read just now.")
        try:
            sample = await self.shared(
                "class_performance",
                lambda: self.services.class_performance(self.authorization, self.classroom, self.correlation),
            )
        except UpstreamError:
            raise unread from None
        try:
            output = self.workspace.class_comparison(sample.students, topic, self.student)
            classmates = max(0, sample.total - 1)
            self.comparison_scope = ClassComparisonScope(
                population=classmates,
                compared=output["students_compared"],
                partial=output["students_compared"] < classmates,
            )
            output["scope"] = self.comparison_scope.model_dump()
            if self.comparison_scope.partial:
                target = f"{target} (subset of classmates)" if topic else "subset of classmates"
            return target, "ok", output
        except (KeyError, TypeError, ValueError, AttributeError):
            raise unread from None

    def words(self, ref: str) -> str | None:
        if result := self.workspace.result(ref):
            return result.title
        if q := self.workspace.questions().get(ref):
            return f"an answer in {q.assessment_title}"
        return super().words(ref)

    def finish(self, report: dict[str, Any], turn: int) -> ResponseContract:
        try:
            checked = checks.report(report, self.workspace, self.shown)
        except checks.RejectedError as e:
            log.info("interpretation_rejected", extra={"turns": turn, "dropped": dict(e.dropped)})
            raise failure(
                422,
                "INTERPRETATION_REJECTED",
                "The drafted interpretation did not rest on this student's evidence, so it was not shown. Try again.",
            ) from None
        dropped = self.turn_drops(checked.dropped)
        cited = {q.ref: q for f in checked.observations + checked.misconceptions for q in f.evidence}
        log.info(
            "interpreted",
            extra={"turns": turn, "tools": len(self.steps), "dropped": dict(dropped), "cited": len(cited)},
        )
        return ResponseContract(
            classroom_id=self.classroom,  # type: ignore[arg-type]
            student_id=self.student,
            observations=[_observation(f) for f in checked.observations],
            possible_misconceptions=[_observation(f) for f in checked.misconceptions],
            suggested_interventions=checked.interventions,
            recommended_topic_ids=checked.topic_ids,  # type: ignore[arg-type]
            evidence=[Evidence(**evidence_fields(q)) for q in cited.values()],
            steps=self.shown_steps(),
            model_turns=turn,
            dropped=dict(dropped),
            class_comparison_scope=self.comparison_scope,
        )


def _observation(f: checks.Finding) -> Observation:
    return Observation(text=f.text, evidence_ids=[q.question_id for q in f.evidence])  # type: ignore[misc]
