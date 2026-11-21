"""class insights (adr 0021): a classroom's teacher asks one question about the class; the agent opens with the class's released topic figures, then each model turn either picks read-only tools or reports, on the loop interpretation uses. the model sees students as references only; names are added after the checks, for the teacher."""

import logging
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any

from fastapi import Request
from learning_transport import RELATION_TEACHER, Claims, UpstreamError, failure, require_relation
from learning_transport.agents import gather_all

import checks
import class_checks
import class_prompts
from class_workspace import RESULTS_SHOWN, ClassQuestion, ClassWorkspace, Student
from clients import Services
from config import Settings
from contracts import (
    CitedStudent,
    ClassComparisonScope,
    ClassEvidence,
    ClassInsightsRequest,
    ClassInsightsResponse,
    ClassMisconception,
    ClassObservation,
    Reteach,
    StrugglingGroup,
)
from loop import Loop, Outcome, ToolError, Wording
from upstream import Assessment
from workspace import MAX_RESULTS, Question, Result, Topic, evidence_fields

log = logging.getLogger("student-performance-agent.class_insights")

NOT_TEACHER = ("FORBIDDEN", "Only the classroom's teacher can ask about its results.")
WORDING = Wording(
    slow="Answering this question took too long. Try again, or try again later.",
    no_access="You no longer have access to this classroom's results.",
    unavailable="Class insights are not available right now. Try again later.",
)
UNNAMED = "Unnamed student"


class ClassAgent(Loop[ClassInsightsResponse]):
    log = log
    event = "class_insights"
    wording = WORDING
    names = ("class_insights.turn", "class_insights.report")
    reference = "ref"
    workspace: ClassWorkspace

    def __init__(self, services: Services, settings: Settings, request: Request, claims: Claims) -> None:
        super().__init__(services, settings, request, claims)
        self.workspace = ClassWorkspace()
        self.classroom = self.question = ""  # set by answer
        self.previous: list[str] = []
        self.display: dict[str, str] = {}  # student id → name, for the teacher's response only
        # assessment or topic id → the batch read that has it, so each is read once per request
        self.assessment_read: dict[str, tuple[str, ...]] = {}
        self.name_read: dict[str, tuple[str, ...]] = {}

    async def answer(self, body: ClassInsightsRequest) -> ClassInsightsResponse:
        return await self.bounded(self._answer(body))

    async def _answer(self, body: ClassInsightsRequest) -> ClassInsightsResponse:
        self.classroom, self.question, self.previous = (
            str(body.classroom_id),
            body.question,
            body.previous_questions,
        )
        await require_relation(
            self.services.identity,
            self.request,
            self.classroom,
            allowed=(RELATION_TEACHER,),
            denied=NOT_TEACHER,
        )
        try:
            await self._open()
        except UpstreamError as e:
            log.warning("class_insights_open_failed", extra={"error": str(e)})
            raise self.refused(e) from None
        if not self.workspace.has_evidence():
            raise failure(
                422,
                "NO_EVIDENCE",
                "This classroom has no released results yet, so there is nothing to answer from.",
            )
        return await self.loop()

    async def _open(self) -> None:
        """the class's released topic metrics, its roster's names (kept here) and its topics' names."""
        sample, roster = await gather_all(
            self.services.class_metrics(self.authorization, self.classroom, self.correlation),
            self.services.roster_names(self.authorization, self.classroom, self.correlation),
        )
        self.workspace.add_class(roster, sample.students, sample.total)
        self.display = {s["id"]: s["display_name"] or s["username"] for s in roster}
        if self.workspace.has_evidence():
            self.workspace.name_topics(await self._topic_names(list(self.workspace.topics)))
        self.workspace.fit()

    # ---- the loop's parts

    def messages(self, turn: int, turns: int, final: bool) -> list[dict[str, str]]:
        return class_prompts.messages(
            self.question, self.previous, self.workspace.opening(), self.history, turn, turns, final
        )

    def schema(self, final: bool) -> dict[str, Any]:
        ws = self.workspace
        return class_prompts.schema(
            final,
            [t.ref for t in ws.topics.values()],
            [s.ref for s in ws.students.values() if s.student_id in ws.sampled],
            [ref for ref in ws.evidence if ref in ws.seen],
        )

    async def tool(self, tool: str, ref: str | None, topic: Topic | None) -> tuple[str, Outcome, Any]:
        if tool == "read_answers":
            return self._read_answers(ref, topic)
        if tool == "student_results":
            return await self._student_results(ref)
        if tool not in ("topic_detail", "topic_questions", "prerequisites"):
            raise ToolError("unknown tool", "refused", f"There is no tool {tool}.")
        if topic is None:
            raise ToolError("no topic", "refused", "Give the topic to look at.")
        if tool == "topic_detail":
            return topic.label, "ok", self.workspace.topic_detail(topic)
        if tool == "topic_questions":
            return await self._topic_questions(topic)
        return await self.prerequisites(topic)

    def _read_answers(self, ref: str | None, topic: Topic | None) -> tuple[str, Outcome, Any]:
        found = None
        if ref and ref.startswith("Q"):
            found = self.workspace.read_question(ref)
        elif ref:
            found = self.workspace.read_result(ref, topic)
        if found is None:
            raise ToolError(
                "unknown question or result",
                "refused",
                "Give a question reference from topic_questions or a result reference from student_results.",
            )
        return found[0], "ok", found[1]

    async def _student_results(self, ref: str | None) -> tuple[str, Outcome, Any]:
        student = self.workspace.student_by_ref(ref)
        if student is None:
            raise ToolError("unknown student", "refused", "Give a student reference from the class overview.")
        target = self._name(student.student_id)
        if student.student_id not in self.workspace.sampled:
            # outside the students the opening read, so outside the answer's scope.
            raise ToolError(
                target, "refused", "This student is outside the students the class figures cover."
            )
        try:
            sample = await self.services.results(
                self.authorization, self.classroom, student.student_id, self.correlation, MAX_RESULTS
            )
            shown = sample.evaluations[:RESULTS_SHOWN]  # the rest are only counted
            assessments = await self._assessments([v["assessment_id"] for v in shown])
        except UpstreamError:
            raise ToolError(target, "failed", "This student's results could not be read just now.") from None
        results = self.workspace.add_student_results(student, shown, assessments)
        await self._name_new_topics()
        output = self.workspace.student_results(student, results, len(sample.evaluations), sample.partial)
        return target, "ok", output

    async def _topic_questions(self, topic: Topic) -> tuple[str, Outcome, Any]:
        try:
            sample = await self.shared(
                "class_evaluations",
                lambda: self.services.class_evaluations(self.authorization, self.classroom, self.correlation),
            )
            wanted = {
                v["assessment_id"]
                for v in sample.evaluations
                if any(e["topic_id"] == topic.topic_id for e in v["evidence"])
            }
            assessments = await self._assessments(list(wanted))
        except UpstreamError:
            raise ToolError(
                topic.label, "failed", "The class's results could not be read just now."
            ) from None
        output = self.workspace.topic_questions(topic, sample.evaluations, assessments, sample.partial)
        return topic.label, "ok", output

    async def _batched[T](
        self,
        kind: str,
        claimed: dict[str, tuple[str, ...]],
        ids: list[str],
        read: Callable[[str, list[str], str], Coroutine[Any, Any, dict[str, T]]],
    ) -> dict[str, T]:
        """`read` of these ids, each in one batch per request however many calls want it at once; a call
        wanting an id already claimed awaits that batch."""
        wanted = list(dict.fromkeys(ids))
        if missing := [i for i in wanted if i not in claimed]:
            claimed.update(dict.fromkeys(missing, tuple(missing)))
        batches = dict.fromkeys(claimed[i] for i in wanted)
        parts = await gather_all(
            *(
                # a failed batch leaves `reads`, so a later call reads it again.
                self.shared(
                    f"{kind} " + " ".join(batch),
                    partial(read, self.authorization, list(batch), self.correlation),
                )
                for batch in batches
            )
        )
        return {i: v for part in parts for i, v in part.items()}

    async def _assessments(self, ids: list[str]) -> dict[str, Assessment]:
        """the teacher's view of these assessments."""
        return await self._batched("assessments", self.assessment_read, ids, self.services.assessments)

    async def _topic_names(self, ids: list[str]) -> dict[str, str]:
        """curriculum's names for these topics; a topic it no longer has is left out, and not asked for again."""
        return await self._batched("names", self.name_read, ids, self.services.topic_names)

    async def _name_new_topics(self) -> None:
        """names for topics a tool brought in; a topic left unnamed is shown as unnamed."""
        try:
            self.workspace.name_topics(await self._topic_names(self.workspace.unnamed()))
        except UpstreamError:
            log.info("topic_names_failed")

    # ---- the report

    def _name(self, student_id: str) -> str:
        return self.display.get(student_id) or UNNAMED

    def words(self, ref: str) -> str | None:
        found = self.workspace.lookup(ref)
        if isinstance(found, Student):
            return self._name(found.student_id)
        if isinstance(found, Result):
            return found.title
        if isinstance(found, ClassQuestion):
            return f"a question in {found.assessment_title}"
        if isinstance(found, Question):
            return f"{self._name(self.workspace.owner[ref])}'s answer in {found.assessment_title}"
        return found.label if found else None

    def finish(self, report: dict[str, Any], turn: int) -> ClassInsightsResponse:
        try:
            checked = class_checks.report(report, self.workspace, self.shown)
        except checks.RejectedError as e:
            log.info("class_insights_rejected", extra={"turns": turn, "dropped": dict(e.dropped)})
            if not e.dropped:
                # nothing dropped: the model found nothing to say, not something unsupported.
                raise failure(
                    422,
                    "NOTHING_TO_ANSWER",
                    "Nothing in this class's released results answers this question yet. Try asking about a "
                    "topic or the class as a whole.",
                ) from None
            raise failure(
                422,
                "INSIGHTS_REJECTED",
                "The drafted answer did not rest on this class's evidence, so it was not shown. Try again.",
            ) from None
        dropped = self.turn_drops(checked.dropped)
        statements = checked.observations + checked.misconceptions + checked.reteach
        cited: dict[str, Question] = {q.ref: q for s in statements for q in s.evidence}
        named = [s.student_id for g in checked.struggling for s in g.students]
        named += [s.student_id for o in checked.observations for s in o.students]
        named += [self.workspace.owner[ref] for ref in cited]
        log.info(
            "class_insights_answered",
            extra={"turns": turn, "tools": len(self.steps), "dropped": dict(dropped), "cited": len(cited)},
        )
        return ClassInsightsResponse(
            classroom_id=self.classroom,  # type: ignore[arg-type]
            question=self.question,
            struggling=[
                StrugglingGroup(
                    topic_id=g.topic.topic_id,  # type: ignore[arg-type]
                    student_ids=[s.student_id for s in g.students],
                    text=g.text,
                )
                for g in checked.struggling
            ],
            observations=[
                ClassObservation(
                    text=o.text,
                    topic_ids=[t.topic_id for t in o.topics],  # type: ignore[misc]
                    student_ids=[s.student_id for s in o.students],
                    evidence_ids=[q.ref for q in o.evidence],
                )
                for o in checked.observations
            ],
            possible_misconceptions=[
                ClassMisconception(text=m.text, evidence_ids=[q.ref for q in m.evidence])
                for m in checked.misconceptions
            ],
            reteach=[
                Reteach(topic_id=r.topics[0].topic_id, text=r.text, evidence_ids=[q.ref for q in r.evidence])  # type: ignore[arg-type]
                for r in checked.reteach
            ],
            students=[CitedStudent(student_id=s, display_name=self._name(s)) for s in dict.fromkeys(named)],
            evidence=[self._evidence(q) for q in cited.values()],
            steps=self.shown_steps(),
            model_turns=turn,
            dropped=dict(dropped),
            scope=ClassComparisonScope(**self.workspace.scope()),
        )

    def _evidence(self, q: Question) -> ClassEvidence:
        return ClassEvidence(evidence_id=q.ref, student_id=self.workspace.owner[q.ref], **evidence_fields(q))
