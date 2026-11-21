"""what the class insights agent knows about one classroom (adr 0021), under short references the model cites ("S1" a student, "T1" a topic, "Q1" a question, "R1" one student's result, "E1" one student's answer to one question). names never enter it: the agent maps students to names after the checks."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from upstream import (
    Assessment,
    Evaluation,
    Evidence,
    MetricStudent,
    RosterStudent,
    TeacherQuestion,
    TopicMetrics,
    TopicName,
)
from workspace import (
    MAX_READ,
    Question,
    Result,
    Topic,
    Workspace,
    clip,
    new_question,
    new_result,
    question_text,
    questions_of,
    title_of,
)

# a student with enough evidence on a topic struggles with it when analytics says they need practice or their
# share of the marks is under this.
WEAK_SHARE = 0.6
# the opening overview's size, in characters of json; longer student lists are cut to fit.
OPENING_BUDGET = 16000
# students one topic_detail call lists, struggling and lowest marks first.
ROWS_SHOWN = 60
# questions one topic_questions call lists, most marks lost first, and lost-mark answers listed under each.
QUESTIONS_SHOWN = 8
LOST_SHOWN = 6
# a student's newest results student_results lists.
RESULTS_SHOWN = 10
# the longest list of student references the opening shows per topic at each level of detail.
LIST_CUTS = (None, None, 25, 10)

Standing = Literal["none", "insufficient", "struggling", "fine"]


@dataclass
class Student:
    ref: str  # "S1", "S2"… in roster order
    student_id: str
    topics: dict[str, TopicMetrics] = field(default_factory=dict)  # released metrics by topic id


@dataclass
class ClassQuestion:
    ref: str  # "Q1", "Q2"…
    question_id: str
    assessment_title: str
    topic_id: str
    question: TeacherQuestion | None
    answers: dict[str, Question] = field(default_factory=dict)  # by "E" reference, once each


def share(m: TopicMetrics) -> float:
    return m["marks_awarded"] / m["marks_possible"] if m["marks_possible"] else 0.0


def standing_of(m: TopicMetrics | None) -> Standing:
    """what a student's released metrics on a topic support saying about them."""
    if m is None:
        return "none"
    if m["evidence_status"] != "observed":
        return "insufficient"
    return "struggling" if m["weakness_indicator"] or share(m) < WEAK_SHARE else "fine"


@dataclass
class ClassWorkspace:
    students: dict[str, Student] = field(default_factory=dict)  # by student id, in roster order
    total: int = 0  # students on analytics' roster
    sampled: set[str] = field(
        default_factory=set
    )  # students whose metrics were read: the class every figure covers
    evidence: dict[str, Question] = field(default_factory=dict)  # by "E" reference
    owner: dict[str, str] = field(default_factory=dict)  # "E" reference → student id
    seen: set[str] = field(default_factory=set)  # "E" references a tool's output has shown the model
    results: dict[str, tuple[Result, str]] = field(default_factory=dict)  # "R" reference → result, student id
    questions: dict[str, ClassQuestion] = field(default_factory=dict)  # by "Q" reference
    view: Workspace = field(default_factory=Workspace)  # topics, and how one answer is shown
    # topic id → the shares its question rows showed the model, and the prerequisites whose figures it saw
    question_shares: dict[str, set[float]] = field(default_factory=dict)
    prerequisites_shown: dict[str, set[str]] = field(default_factory=dict)
    _opening: dict[str, Any] = field(default_factory=dict)  # the most detailed opening that fits
    _figures: dict[str, dict[str, Any]] = field(default_factory=dict)  # topic id → its fixed class figures
    _by_ref: dict[str, Student] = field(default_factory=dict)  # students by "S" reference
    _answers: dict[tuple[str, str], str] = field(
        default_factory=dict
    )  # (evaluation, question) → "E" reference
    _results: dict[str, str] = field(default_factory=dict)  # evaluation id → "R" reference
    _questions: dict[tuple[str, str], str] = field(
        default_factory=dict
    )  # (assessment, question) → "Q" reference

    # ---- building it

    @property
    def topics(self) -> dict[str, Topic]:
        return self.view.topics

    @property
    def compared(self) -> int:
        """students whose metrics were read."""
        return len(self.sampled)

    def add_class(self, roster: list[RosterStudent], sample: list[MetricStudent], total: int) -> None:
        """students in roster order with the metrics analytics gave for its first ones."""
        for entry in roster:
            self._student(entry["id"])
        for s in sample:
            self.sampled.add(s["student_id"])
            student = self._student(s["student_id"])
            for m in s["topics"]:
                student.topics[m["topic_id"]] = m
                self.view.topic(m["topic_id"])
        self.total = total

    def _student(self, student_id: str) -> Student:
        if student_id not in self.students:
            student = self.students[student_id] = Student(f"S{len(self.students) + 1}", student_id)
            self._by_ref[student.ref] = student
        return self.students[student_id]

    def name_topics(self, names: dict[str, str]) -> None:
        self.view.name_topics(names)

    def unnamed(self) -> list[str]:
        return [t.topic_id for t in self.topics.values() if not t.name]

    def _answer_of(
        self, v: Evaluation, e: Evidence, assessment: Assessment | None, student_id: str
    ) -> Question:
        """one student's answer to one question, under one "E" reference however often a tool reaches it."""
        key = (v["id"], e["question_id"])
        if key in self._answers:
            return self.evidence[self._answers[key]]
        self.view.topic(e["topic_id"])
        q = new_question(
            f"E{len(self.evidence) + 1}",
            v,
            e,
            title_of(assessment),
            questions_of(assessment),
        )
        self.evidence[q.ref] = q
        self.owner[q.ref] = student_id
        self._answers[key] = q.ref
        return q

    # ---- lookups

    def student_by_ref(self, ref: object) -> Student | None:
        return self._by_ref.get(ref) if isinstance(ref, str) else None

    def topic_by_ref(self, ref: object) -> Topic | None:
        return self.view.topic_by_ref(ref)

    def lookup(self, ref: str) -> Student | Topic | ClassQuestion | Result | Question | None:
        """what a student, topic, question, result or answer reference this workspace gave out stands for."""
        result = self.results.get(ref)
        return (
            self._by_ref.get(ref)
            or self.view.by_ref.get(ref)
            or self.questions.get(ref)
            or self.evidence.get(ref)
            or (result[0] if result else None)
        )

    def is_reference(self, ref: str) -> bool:
        return self.lookup(ref) is not None

    def standing(self, student_id: str, topic_id: str) -> Standing:
        """what a student's released evidence on a topic supports saying about them."""
        student = self.students.get(student_id)
        return standing_of(student.topics.get(topic_id) if student else None)

    def shares(self, topic_ids: set[str], student_ids: set[str], evidence: list[Question]) -> set[float]:
        """the shares a statement about these topics, students and answers may quote (adr 0021): the class's share
        of the marks and the share of students who struggle on each topic, those topics' shown prerequisites
        included; each student's marks and recent marks share on them; the question rows topic_questions showed on
        them, as lost-mark and kept shares; and each cited answer's own marks. a cited answer adds its topic and
        its student."""
        topics = topic_ids | {q.topic_id for q in evidence}
        topics |= {p for t in list(topics) for p in self.prerequisites_shown.get(t, ())}
        students = student_ids | {self.owner[q.ref] for q in evidence if q.ref in self.owner}
        found = {q.awarded / q.possible for q in evidence if q.possible}
        for t in topics:
            row = self._class_topic(t) if t in self.topics else None
            if row and row["class_marks_share"] is not None:
                found.add(row["class_marks_share"])
            if row and row["students_with_evidence"]:
                found.add(row["struggling_count"] / row["students_with_evidence"])
            found |= self.question_shares.get(t, set())
            for sid in students:
                m = self.students[sid].topics.get(t) if sid in self.students else None
                if m is not None:
                    found.add(share(m))
                    if (recent := m.get("recent_marks_accuracy")) is not None:
                        found.add(recent)
        return found

    def has_evidence(self) -> bool:
        return any(s.topics for s in self.students.values())

    def _class_topic(self, topic_id: str) -> dict[str, Any]:
        """the class's pooled marks on a topic and who struggles with it or has too little evidence; the
        figures are fixed once the class is added, so they are counted once, and the name is read each time."""
        if topic_id not in self._figures:
            awarded = possible = learners = 0
            struggling, insufficient = [], []
            for s in self.students.values():
                m = s.topics.get(topic_id)
                if m is None:
                    continue
                awarded, possible, learners = (
                    awarded + m["marks_awarded"],
                    possible + m["marks_possible"],
                    learners + 1,
                )
                standing = standing_of(m)
                if standing == "struggling":
                    struggling.append(s.ref)
                elif standing == "insufficient":
                    insufficient.append(s.ref)
            self._figures[topic_id] = {
                "students_with_evidence": learners,
                "class_marks_share": round(awarded / possible, 4) if possible else None,
                "struggling_count": len(struggling),
                "struggling": struggling,
                "insufficient_evidence_count": len(insufficient),
                "insufficient_evidence": insufficient,
            }
        return {
            "topic": self.topics[topic_id].ref,
            "name": self.view.topic_name(topic_id),
            **self._figures[topic_id],
        }

    # ---- what the model sees

    def fit(self) -> None:
        """the most detailed opening that fits its budget, or the least detailed; run once, before the first turn."""
        for detail in range(len(LIST_CUTS)):
            self._opening = self._overview(detail)
            if len(json.dumps(self._opening, ensure_ascii=False)) <= OPENING_BUDGET:
                break

    def opening(self) -> dict[str, Any]:
        """the opening fit() chose."""
        return self._opening

    def _overview(self, detail: int) -> dict[str, Any]:
        """the class's released topic figures, without any answers or names."""
        with_evidence = [s for s in self.students.values() if s.topics]
        taught = list(dict.fromkeys(t for s in with_evidence for t in s.topics))
        cut = LIST_CUTS[detail]
        topics = []
        for topic_id in taught:
            row = self._class_topic(topic_id)
            if cut is not None:
                for key in ("struggling", "insufficient_evidence"):
                    row[key] = row[key][:cut]
            topics.append(row)
        overview: dict[str, Any] = {
            "class": self.scope(),
            "students_with_evidence": len(with_evidence),
            "topics": topics,
        }
        if detail == 0:
            overview["students"] = [
                {
                    "student": s.ref,
                    "topics_with_evidence": len(s.topics),
                    "struggling_on": [
                        self.topics[t].ref for t, m in s.topics.items() if standing_of(m) == "struggling"
                    ],
                }
                for s in with_evidence
            ]
        return overview

    def scope(self) -> dict[str, Any]:
        return {
            "population": self.total,
            "compared": self.compared,
            "partial": self.compared < self.total,
            "selection": "roster_order",
        }

    def topic_detail(self, topic: Topic) -> dict[str, Any]:
        """every student's released evidence on one topic, those who struggle and score lowest first."""
        rows = []
        for s in self.students.values():
            m = s.topics.get(topic.topic_id)
            if m is None:
                continue
            rows.append(
                (
                    standing_of(m) != "struggling",
                    share(m),
                    {
                        "student": s.ref,
                        "questions": m["attempted"],
                        "full_marks": m["correct"],
                        "marks": f"{m['marks_awarded']}/{m['marks_possible']}",
                        "recent_marks_share": m.get("recent_marks_accuracy"),
                        "evidence": m["evidence_status"],
                        "needs_practice": m["weakness_indicator"],
                    },
                )
            )
        rows.sort(key=lambda r: (r[0], r[1]))
        return {
            **self._class_topic(topic.topic_id),
            "students": [r[2] for r in rows[:ROWS_SHOWN]],
            "not_shown": max(0, len(rows) - ROWS_SHOWN),
        }

    def topic_questions(
        self, topic: Topic, evaluations: list[Any], assessments: dict[str, Assessment], partial: bool
    ) -> dict[str, Any]:
        """the class's finalized answers to each question on a topic: how many tried it, the marks lost, and some answers that lost marks. `partial` when only the class's newest evaluations were read."""
        for v in evaluations:
            if v["student_id"] not in self.sampled:
                continue  # outside the students the opening read, so outside the answer's scope
            assessment = assessments.get(v["assessment_id"])
            for e in v["evidence"]:
                if e["topic_id"] == topic.topic_id:
                    a = self._answer_of(v, e, assessment, v["student_id"])
                    self._class_question(v, e, assessment).answers.setdefault(a.ref, a)
        on_topic = [q for q in self.questions.values() if q.topic_id == topic.topic_id]
        on_topic.sort(
            key=lambda q: -sum(a.possible - a.awarded for a in q.answers.values()) / max(1, len(q.answers))
        )
        return {
            "topic": topic.ref,
            "questions": [self._question_row(q) for q in on_topic[:QUESTIONS_SHOWN]],
            "not_shown": max(0, len(on_topic) - QUESTIONS_SHOWN),
            "older_results_not_read": partial,
        }

    def _class_question(self, v: Evaluation, e: Evidence, assessment: Assessment | None) -> ClassQuestion:
        key = (v["assessment_id"], e["question_id"])
        if key not in self._questions:
            q = ClassQuestion(
                f"Q{len(self.questions) + 1}",
                e["question_id"],
                title_of(assessment),
                e["topic_id"],
                questions_of(assessment).get(e["question_id"]),
            )
            self.questions[q.ref] = q
            self._questions[key] = q.ref
        return self.questions[self._questions[key]]

    def _question_row(self, q: ClassQuestion) -> dict[str, Any]:
        kind, prompt, expected = question_text(q.question or {})
        answers = list(q.answers.values())
        lost = sorted((a for a in answers if a.lost_marks), key=lambda a: (a.awarded - a.possible, a.ref))
        shown = lost[:LOST_SHOWN]
        self.seen.update(a.ref for a in shown)
        row: dict[str, Any] = {
            "question": q.ref,
            "assessment": q.assessment_title,
            "type": kind,
            "prompt": prompt,
            "students_answered": len(q.answers),
            "students_lost_marks": len(lost),
            "mean_marks_lost": round(sum(a.possible - a.awarded for a in answers) / len(answers), 2),
            "possible": max(a.possible for a in answers),
            "lost_marks": [
                {
                    "evidence": a.ref,
                    "student": self.students[self.owner[a.ref]].ref,
                    "marks": f"{a.awarded}/{a.possible}",
                }
                for a in shown
            ],
        }
        if expected is not None:
            row["expected"] = expected
        lost_share = row["students_lost_marks"] / row["students_answered"]
        marks_lost = row["mean_marks_lost"] / row["possible"] if row["possible"] else 0.0
        self.question_shares.setdefault(q.topic_id, set()).update(
            {lost_share, 1 - lost_share, marks_lost, 1 - marks_lost}
        )
        return row

    def add_student_results(
        self, student: Student, evaluations: list[Evaluation], assessments: dict[str, Assessment]
    ) -> list[Result]:
        """the student's newest results, each under one "R" reference however often a tool reads it."""
        found = []
        for v in evaluations[:RESULTS_SHOWN]:
            ref = self._results.get(v["id"])
            if ref is None:
                assessment = assessments.get(v["assessment_id"])
                questions = [self._answer_of(v, e, assessment, student.student_id) for e in v["evidence"]]
                result = new_result(f"R{len(self.results) + 1}", v, title_of(assessment), questions)
                self.results[result.ref] = (result, student.student_id)
                self._results[v["id"]] = ref = result.ref
            found.append(self.results[ref][0])
        return found

    def student_results(
        self, student: Student, results: list[Result], read: int, older: bool
    ) -> dict[str, Any]:
        """one student's released topic figures and newest finalized results, marks only; `read` counts the results read, and `older` says older ones were left unread."""
        rows = []
        for result in results:
            self.seen.update(q.ref for q in result.questions)
            rows.append(
                {
                    "result": result.ref,
                    "assessment": result.title,
                    "finalized": result.finalized_at,
                    "released": result.released,
                    "questions": [
                        {
                            "evidence": q.ref,
                            "topic": self.topics[q.topic_id].ref,
                            "marks": f"{q.awarded}/{q.possible}",
                        }
                        for q in result.questions
                    ],
                }
            )
        return {
            "student": student.ref,
            "topics": [
                {
                    "topic": self.topics[t].ref,
                    "marks": f"{m['marks_awarded']}/{m['marks_possible']}",
                    "evidence": m["evidence_status"],
                    "needs_practice": m["weakness_indicator"],
                    "name": self.view.topic_name(t),
                }
                for t, m in student.topics.items()
            ],
            "results": rows,
            "not_shown": max(0, read - len(results)),
            "older_results_not_read": older,
        }

    def read_result(self, ref: str, topic: Topic | None) -> tuple[str, dict[str, Any]] | None:
        """one result's answers, lost marks first, and the title it is known by; none for an unknown result."""
        if ref not in self.results:
            return None
        result, student_id = self.results[ref]
        output = {"student": self.students[student_id].ref, **self.view.read_answers(result, topic)}
        self.seen.update(q["evidence"] for q in output["questions"])
        return result.title, output

    def read_question(self, ref: str) -> tuple[str, dict[str, Any]] | None:
        """the class's answers to one question, lost marks first, at most max_read; none for an unknown question."""
        if ref not in self.questions:
            return None
        q = self.questions[ref]
        chosen = sorted(q.answers.values(), key=lambda a: (not a.lost_marks, a.awarded - a.possible, a.ref))
        shown = chosen[:MAX_READ]
        self.seen.update(a.ref for a in shown)
        output = {
            "question": q.ref,
            "assessment": q.assessment_title,
            "answers": [
                {"student": self.students[self.owner[a.ref]].ref, **self.view.answer(a)} for a in shown
            ],
            "not_shown": len(chosen) - len(shown),
        }
        return q.assessment_title, output

    def prerequisites(self, topic: Topic, prerequisites: list[TopicName]) -> dict[str, Any]:
        """the topics one topic builds on, with the class's released figures on each."""
        rows = []
        self.prerequisites_shown.setdefault(topic.topic_id, set()).update(p["id"] for p in prerequisites)
        for p in prerequisites:
            ref = self.view.topic(p["id"], p["name"])
            row = self._class_topic(p["id"])
            rows.append(
                {
                    "topic": ref.ref,
                    "name": clip(p["name"], 160),
                    "students_with_evidence": row["students_with_evidence"],
                    "class_marks_share": row["class_marks_share"],
                    "struggling": row["struggling"],
                }
            )
        return {"topic": topic.ref, "prerequisites": rows}
