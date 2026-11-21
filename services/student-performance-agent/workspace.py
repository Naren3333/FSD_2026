"""what the agent knows about one student, under short references the model cites ("R1" a result, "E1" a question's evidence, "T1" a topic), and the tools that add to it."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from upstream import (
    Assessment,
    ClassStudent,
    CriterionScore,
    Evaluation,
    Evidence,
    TeacherQuestion,
    TopicMetrics,
    TopicName,
)

# the longest text of a prompt, answer, key or feedback the model is shown.
MAX_FIELD = 300
# results the opening overview lists, newest first; older ones stay out of the model's reach.
MAX_RESULTS = 20
# questions one read_answers call shows.
MAX_READ = 8
# the opening overview's size, in characters of json; older results are left out beyond it.
OPENING_BUDGET = 16000


def clip(text: object, limit: int = MAX_FIELD) -> str:
    value = text if isinstance(text, str) else ""
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


@dataclass
class Question:
    ref: str  # "E1", "E2"…
    question_id: str
    evaluation_id: str
    assessment_id: str
    assessment_title: str
    topic_id: str
    awarded: int
    possible: int
    question: TeacherQuestion | None  # the teacher's view from assessment; empty when it was not found
    answer: str
    feedback: str
    criteria: list[CriterionScore]  # grading's criterion scores

    @property
    def lost_marks(self) -> bool:
        return self.awarded < self.possible


@dataclass
class Result:
    ref: str  # "R1", "R2"…
    evaluation_id: str
    assessment_id: str
    title: str
    finalized_at: str
    late: bool
    released: bool  # the student can see it; analytics' figures count released results only
    questions: list[Question]


@dataclass
class Topic:
    ref: str  # "T1", "T2"…
    topic_id: str
    name: str

    @property
    def label(self) -> str:
        """the topic as a step names it to the teacher."""
        return self.name or "a topic"


def title_of(assessment: Assessment | None) -> str:
    return clip(assessment["title"] if assessment else "Deleted assessment", 160)


def questions_of(assessment: Assessment | None) -> dict[str, TeacherQuestion]:
    """the assessment's questions by id; none once it is deleted."""
    return {q["id"]: q for q in assessment["questions"]} if assessment else {}


def new_question(
    ref: str, v: Evaluation, e: Evidence, title: str, questions: Mapping[str, TeacherQuestion]
) -> Question:
    """one answer of a finalized evaluation, joined to the teacher's question."""
    return Question(
        ref,
        e["question_id"],
        v["id"],
        v["assessment_id"],
        title,
        e["topic_id"],
        e["awarded"],
        e["possible"],
        questions.get(e["question_id"]),
        (v.get("answers") or {}).get(e["question_id"], ""),
        e.get("feedback") or "",
        e.get("criteria") or [],
    )


def new_result(ref: str, v: Evaluation, title: str, questions: list[Question]) -> Result:
    return Result(
        ref,
        v["id"],
        v["assessment_id"],
        title,
        (v.get("finalized_at") or "")[:10],
        v.get("late", False),
        bool(v.get("released_at")),
        questions,
    )


def question_text(question: Mapping[str, Any]) -> tuple[str, str, str | None]:
    """a question's type, prompt and expected answer as the model is shown them; none when it has no answer."""
    expected = question.get("answer") or "; ".join(question.get("accepted_answers") or [])
    expected = expected or (question.get("rubric") or {}).get("model_answer")
    return (
        question.get("type") or "unknown",
        clip(question.get("prompt")) or "(question no longer available)",
        clip(expected) if expected else None,
    )


def evidence_fields(q: Question) -> dict[str, Any]:
    """the fields a response's evidence item takes from a cited question."""
    return {
        "question_id": q.question_id,
        "evaluation_id": q.evaluation_id,
        "assessment_id": q.assessment_id,
        "assessment_title": q.assessment_title,
        "topic_id": q.topic_id,
        "prompt": str((q.question["prompt"] if q.question else "") or ""),
        "awarded": q.awarded,
        "possible": q.possible,
    }


@dataclass
class Workspace:
    results: list[Result] = field(default_factory=list)
    topics: dict[str, Topic] = field(default_factory=dict)  # by topic id
    summary: list[TopicMetrics] = field(default_factory=list)  # analytics' topic metrics
    by_ref: dict[str, Topic] = field(default_factory=dict)  # the same topics by "T" reference

    # ---- building it

    def topic(self, topic_id: str, name: str = "") -> Topic:
        name = clip(name, 160)
        if topic_id not in self.topics:
            topic = self.topics[topic_id] = Topic(f"T{len(self.topics) + 1}", topic_id, name)
            self.by_ref[topic.ref] = topic
        elif name and not self.topics[topic_id].name:
            self.topics[topic_id].name = name
        return self.topics[topic_id]

    def add_results(self, evaluations: list[Evaluation], assessments: dict[str, Assessment]) -> None:
        """join validated finalized evaluations to the teacher's assessment snapshots."""
        count = 0
        for v in evaluations[:MAX_RESULTS]:
            assessment = assessments.get(v["assessment_id"])
            title, by_id = title_of(assessment), questions_of(assessment)
            questions = []
            for e in v["evidence"]:
                count += 1
                self.topic(e["topic_id"])
                questions.append(new_question(f"E{count}", v, e, title, by_id))
            self.results.append(new_result(f"R{len(self.results) + 1}", v, title, questions))

    def add_summary(self, topics: list[TopicMetrics]) -> None:
        """validated analytics rows used by the overview and tools."""
        self.summary = topics
        for t in self.summary:
            self.topic(t["topic_id"])

    def name_topics(self, names: dict[str, str]) -> None:
        for topic_id, name in names.items():
            self.topic(topic_id, name)

    # ---- lookups

    def questions(self) -> dict[str, Question]:
        return {q.ref: q for r in self.results for q in r.questions}

    def result(self, ref: object) -> Result | None:
        return next((r for r in self.results if r.ref == ref), None)

    def topic_by_ref(self, ref: object) -> Topic | None:
        return self.by_ref.get(ref) if isinstance(ref, str) else None

    def topic_name(self, topic_id: str) -> str:
        topic = self.topics.get(topic_id)
        return topic.name if topic and topic.name else "an unnamed topic"

    def _metrics(self, topic_id: str) -> TopicMetrics | None:
        return next((t for t in self.summary if t["topic_id"] == topic_id), None)

    # ---- what the model sees

    def fit(self) -> None:
        """leaves out the oldest results until the opening fits; a result left out cannot be read. run once, before the first turn, so a result the model has seen never disappears as tools add topics."""
        while (
            len(self.results) > 1 and len(json.dumps(self._overview(), ensure_ascii=False)) > OPENING_BUDGET
        ):
            self.results.pop()

    def opening(self) -> dict[str, Any]:
        """the student's topic summary and results, without any answers."""
        return self._overview()

    def _overview(self) -> dict[str, Any]:
        return {
            "topic_summary": [self._summary_row(t) for t in self.summary],
            "results": [
                {
                    "result": r.ref,
                    "assessment": r.title,
                    "finalized": r.finalized_at,
                    "late": r.late,
                    "released": r.released,
                    "questions": [
                        {
                            "evidence": q.ref,
                            "topic": self.topics[q.topic_id].ref,
                            "type": q.question["type"] if q.question else "unknown",
                            "marks": f"{q.awarded}/{q.possible}",
                        }
                        for q in r.questions
                    ],
                }
                for r in self.results
            ],
            "topics": [{"topic": t.ref, "name": t.name or "(unnamed)"} for t in self.topics.values()],
        }

    def _summary_row(self, t: TopicMetrics) -> dict[str, Any]:
        return {
            "topic": self.topics[t["topic_id"]].ref,
            "name": self.topic_name(t["topic_id"]),
            "questions": t["attempted"],
            "full_marks": t["correct"],
            "marks": f"{t['marks_awarded']}/{t['marks_possible']}",
            "observed_accuracy": t["observed_accuracy"],
            "evidence": t["evidence_status"],
            "recent_marks_share": t.get("recent_marks_accuracy"),
            "needs_practice": t["weakness_indicator"],
        }

    def read_answers(self, result: Result, topic: Topic | None) -> dict[str, Any]:
        """up to max_read of the result's questions, those that lost marks first, with the student's answers."""
        chosen = [q for q in result.questions if topic is None or q.topic_id == topic.topic_id]
        chosen.sort(key=lambda q: (not q.lost_marks, q.ref))
        shown = chosen[:MAX_READ]
        return {
            "assessment": result.title,
            "questions": [self.answer(q) for q in shown],
            "not_shown": len(chosen) - len(shown),
        }

    def answer(self, q: Question) -> dict[str, Any]:
        question: Mapping[str, Any] = q.question or {}
        kind, prompt, expected = question_text(question)
        row: dict[str, Any] = {
            "evidence": q.ref,
            "topic": self.topics[q.topic_id].ref,
            "type": kind,
            "prompt": prompt,
            "student_answer": clip(q.answer) or "(blank)",
            "marks": f"{q.awarded}/{q.possible}",
        }
        if question.get("options"):
            row["options"] = [clip(o, 120) for o in question["options"]]
        if expected is not None:
            row["expected"] = expected
        rubric = question.get("rubric") or {}
        if rubric.get("criteria") and q.criteria:
            earned = {c.get("criterion_id"): c.get("points") for c in q.criteria}
            row["rubric"] = [
                {
                    "criterion": clip(c.get("title"), 120),
                    "earned": earned.get(c.get("id")),
                    "max": max((level.get("points", 0) for level in c.get("levels") or []), default=0),
                }
                for c in rubric["criteria"]
            ]
        if q.feedback:
            row["teacher_feedback"] = clip(q.feedback)
        return row

    def prerequisites(self, topic: Topic, prerequisites: list[TopicName]) -> dict[str, Any]:
        rows = []
        for p in prerequisites:
            ref = self.topic(p["id"], p["name"])
            metrics = self._metrics(p["id"])
            rows.append(
                {
                    "topic": ref.ref,
                    "name": clip(p["name"], 160),
                    "student": self._summary_row(metrics) if metrics else "no released evidence",
                }
            )
        return {"topic": topic.ref, "prerequisites": rows}

    def class_comparison(
        self, students: list[ClassStudent], topic: Topic | None, student_id: str
    ) -> dict[str, Any]:
        """the rest of the class's pooled accuracy on each of the student's topics, next to the student's own; the student is left out so their own marks do not pull the class figure toward theirs."""
        students = [s for s in students if s.get("student_id") != student_id]
        wanted = [t for t in self.summary if topic is None or t["topic_id"] == topic.topic_id]
        ids = {t["topic_id"] for t in wanted}
        pooled: dict[str, tuple[int, int, int]] = {}
        for s in students:
            for m in s.get("topics") or []:
                if (topic_id := m.get("topic_id")) in ids:
                    attempted, correct, learners = pooled.get(topic_id, (0, 0, 0))
                    pooled[topic_id] = (attempted + m["attempted"], correct + m["correct"], learners + 1)
        rows = []
        for t in wanted:
            attempted, correct, learners = pooled.get(t["topic_id"], (0, 0, 0))
            rows.append(
                {
                    "topic": self.topics[t["topic_id"]].ref,
                    "student_accuracy": t["observed_accuracy"],
                    "class_accuracy": round(correct / attempted, 4) if attempted else None,
                    "students_with_evidence": learners,
                }
            )
        return {"students_compared": len(students), "topics": rows}
