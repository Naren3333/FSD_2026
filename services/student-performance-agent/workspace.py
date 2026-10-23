"""what the agent knows about one student, under short references the model cites ("R1" a result, "E1" a question's evidence, "T1" a topic), and the tools that add to it."""

import json
from dataclasses import dataclass, field
from typing import Any

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
    question: dict[str, Any]  # the teacher's view from assessment; empty when it was not found
    answer: str
    feedback: str
    criteria: list[dict[str, Any]]  # grading's criterion scores

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
    questions: list[Question]


@dataclass
class Topic:
    ref: str  # "T1", "T2"…
    topic_id: str
    name: str


@dataclass
class Workspace:
    results: list[Result] = field(default_factory=list)
    topics: dict[str, Topic] = field(default_factory=dict)  # by topic id
    summary: list[dict[str, Any]] = field(default_factory=list)  # analytics' topic metrics
    read: set[str] = field(default_factory=set)  # evidence refs whose answers the model has seen

    # ---- building it

    def topic(self, topic_id: str, name: str = "") -> Topic:
        if topic_id not in self.topics:
            self.topics[topic_id] = Topic(f"T{len(self.topics) + 1}", topic_id, name)
        elif name and not self.topics[topic_id].name:
            self.topics[topic_id].name = name
        return self.topics[topic_id]

    def add_results(self, evaluations: list[dict[str, Any]], assessments: dict[str, Any]) -> None:
        """grading's finalized evaluations, newest first, joined with their assessments' questions."""
        count = 0
        for v in evaluations[:MAX_RESULTS]:
            assessment: dict[str, Any] = assessments.get(v.get("assessment_id", "")) or {}
            by_id: dict[str, dict[str, Any]] = {
                str(q.get("id")): q for q in assessment.get("questions") or [] if isinstance(q, dict)
            }
            answers: dict[str, Any] = v["answers"] if isinstance(v.get("answers"), dict) else {}
            questions = []
            for e in v.get("evidence") or []:
                count += 1
                self.topic(e["topic_id"])
                questions.append(
                    Question(
                        f"E{count}",
                        e["question_id"],
                        v["id"],
                        v["assessment_id"],
                        clip(assessment.get("title") or "Deleted assessment", 160),
                        e["topic_id"],
                        int(e["awarded"]),
                        int(e["possible"]),
                        by_id.get(e["question_id"]) or {},
                        str(answers.get(e["question_id"]) or ""),
                        str(e.get("feedback") or ""),
                        [c for c in e.get("criteria") or [] if isinstance(c, dict)],
                    )
                )
            self.results.append(
                Result(
                    f"R{len(self.results) + 1}",
                    v["id"],
                    v["assessment_id"],
                    clip(assessment.get("title") or "Deleted assessment", 160),
                    str(v.get("finalized_at") or "")[:10],
                    bool(v.get("late")),
                    questions,
                )
            )

    def add_summary(self, topics: list[dict[str, Any]]) -> None:
        self.summary = topics
        for t in topics:
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
        return next((t for t in self.topics.values() if t.ref == ref), None)

    def topic_name(self, topic_id: str) -> str:
        topic = self.topics.get(topic_id)
        return topic.name if topic and topic.name else "an unnamed topic"

    def _metrics(self, topic_id: str) -> dict[str, Any] | None:
        return next((t for t in self.summary if t["topic_id"] == topic_id), None)

    # ---- what the model sees

    def opening(self) -> dict[str, Any]:
        """the student's topic summary and results, without any answers; the oldest results are left out when they would not fit, and a result left out cannot be read."""
        while True:
            opening = self._overview()
            if len(json.dumps(opening, ensure_ascii=False)) <= OPENING_BUDGET or len(self.results) <= 1:
                return opening
            self.results.pop()

    def _overview(self) -> dict[str, Any]:
        return {
            "topic_summary": [self._summary_row(t) for t in self.summary],
            "results": [
                {
                    "result": r.ref,
                    "assessment": r.title,
                    "finalized": r.finalized_at,
                    "late": r.late,
                    "questions": [
                        {
                            "evidence": q.ref,
                            "topic": self.topics[q.topic_id].ref,
                            "type": q.question.get("type") or "unknown",
                            "marks": f"{q.awarded}/{q.possible}",
                        }
                        for q in r.questions
                    ],
                }
                for r in self.results
            ],
            "topics": [{"topic": t.ref, "name": t.name or "(unnamed)"} for t in self.topics.values()],
        }

    def _summary_row(self, t: dict[str, Any]) -> dict[str, Any]:
        return {
            "topic": self.topics[t["topic_id"]].ref,
            "name": self.topic_name(t["topic_id"]),
            "questions": t["attempted"],
            "full_marks": t["correct"],
            "marks": f"{t['marks_awarded']}/{t['marks_possible']}",
            "observed_accuracy": t["observed_accuracy"],
            "evidence": t["evidence_status"],
            "needs_practice": t["weakness_indicator"],
        }

    def read_answers(self, result: Result, topic: Topic | None) -> dict[str, Any]:
        """up to max_read of the result's questions, those that lost marks first, with the student's answers."""
        chosen = [q for q in result.questions if topic is None or q.topic_id == topic.topic_id]
        chosen.sort(key=lambda q: (not q.lost_marks, q.ref))
        shown = chosen[:MAX_READ]
        self.read.update(q.ref for q in shown)
        return {
            "assessment": result.title,
            "questions": [self._answer(q) for q in shown],
            "not_shown": len(chosen) - len(shown),
        }

    def _answer(self, q: Question) -> dict[str, Any]:
        question = q.question
        row: dict[str, Any] = {
            "evidence": q.ref,
            "topic": self.topics[q.topic_id].ref,
            "type": question.get("type") or "unknown",
            "prompt": clip(question.get("prompt")) or "(question no longer available)",
            "student_answer": clip(q.answer) or "(blank)",
            "marks": f"{q.awarded}/{q.possible}",
        }
        if question.get("options"):
            row["options"] = [clip(o, 120) for o in question["options"]]
        expected = question.get("answer") or "; ".join(question.get("accepted_answers") or [])
        rubric = question.get("rubric") or {}
        if expected:
            row["expected"] = clip(expected)
        elif rubric.get("model_answer"):
            row["expected"] = clip(rubric["model_answer"])
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

    def prerequisites(self, topic: Topic, prerequisites: list[dict[str, str]]) -> dict[str, Any]:
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

    def class_comparison(self, students: list[dict[str, Any]], topic: Topic | None) -> dict[str, Any]:
        """the class's pooled accuracy on each of the student's topics, next to the student's own."""
        wanted = [t for t in self.summary if topic is None or t["topic_id"] == topic.topic_id]
        rows = []
        for t in wanted:
            attempted = correct = learners = 0
            for s in students:
                for m in s.get("topics") or []:
                    if m.get("topic_id") == t["topic_id"]:
                        attempted, correct, learners = (
                            attempted + m["attempted"],
                            correct + m["correct"],
                            learners + 1,
                        )
            rows.append(
                {
                    "topic": self.topics[t["topic_id"]].ref,
                    "student_accuracy": t["observed_accuracy"],
                    "class_accuracy": round(correct / attempted, 4) if attempted else None,
                    "students_with_evidence": learners,
                }
            )
        return {"students_compared": len(students), "topics": rows}
