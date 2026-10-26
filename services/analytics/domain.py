from collections import defaultdict
from typing import Any, Literal
from uuid import UUID

from learning_transport import Contract
from learning_transport.kafka import Envelope
from pydantic import Field, model_validator

FINALIZED = "grading.finalized.v3"
# a topic's needs-practice flag reads its latest questions only, so a student who improved loses it.
RECENT_QUESTIONS = 10
# fewer questions than this is too little evidence to report on or to flag.
MIN_EVIDENCE = 5
# needs practice: under this share of the marks on the topic's latest questions.
WEAKNESS_MARKS = 0.6


class Evidence(Contract):
    """one question's finalized marks."""

    question_id: UUID
    topic_id: UUID
    correct: bool
    awarded: int = Field(ge=0, le=100, strict=True)
    possible: int = Field(ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def consistent(self) -> "Evidence":
        if self.awarded > self.possible:
            raise ValueError("Awarded more than possible")
        if self.correct != (self.awarded == self.possible):
            raise ValueError("Correctness and awarded score disagree")
        return self


class Finalized(Contract):
    evaluation_id: UUID
    assessment_id: UUID
    classroom_id: UUID
    org_id: str = Field(min_length=1)
    student_id: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1, max_length=50)
    # each reopen raises it (adr 0016); events from before carry none and are revision 1.
    revision: int = Field(default=1, ge=1, strict=True)

    @model_validator(mode="after")
    def unique_questions(self) -> "Finalized":
        if len({e.question_id for e in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate question evidence")
        return self


class Event(Envelope):
    data: Finalized

    @model_validator(mode="after")
    def finalized(self) -> "Event":
        if self.event_type != FINALIZED or self.resource_id != self.data.evaluation_id:
            raise ValueError("Not a finalized grading event")
        return self


class AssessmentDeleted(Contract):
    assessment_id: UUID
    classroom_id: UUID
    org_id: str = Field(min_length=1)


class AssessmentDeletedEvent(Envelope):
    """a published assessment its teacher deleted with its submissions (adr 0008)."""

    event_type: Literal["assessment.deleted.v1"]
    data: AssessmentDeleted

    @model_validator(mode="after")
    def deleted(self) -> "AssessmentDeletedEvent":
        if self.resource_id != self.data.assessment_id:
            raise ValueError("Not this assessment's deletion")
        return self


class TopicMetric(Contract):
    """a topic's finalized evidence: questions attempted and answered with full marks, and the marks earned out of the marks possible (which shows part marks). the needs-practice flag reads the share of marks on the topic's latest ten questions."""

    topic_id: UUID
    attempted: int = Field(ge=1)
    correct: int = Field(ge=0)
    observed_accuracy: float = Field(ge=0, le=1)
    marks_awarded: int = Field(ge=0)
    marks_possible: int = Field(ge=1)
    recent_attempted: int = Field(ge=1, le=RECENT_QUESTIONS)
    recent_marks_accuracy: float = Field(ge=0, le=1)
    evidence_status: Literal["insufficient_evidence", "observed"]
    weakness_indicator: bool

    @model_validator(mode="after")
    def evidence_matches(self) -> "TopicMetric":
        if self.correct > self.attempted or self.observed_accuracy != round(self.correct / self.attempted, 4):
            raise ValueError("Accuracy must match the observed counts")
        if self.marks_awarded > self.marks_possible or self.marks_possible < self.attempted:
            raise ValueError("Marks must match the observed questions")
        if self.recent_attempted != min(self.attempted, RECENT_QUESTIONS):
            raise ValueError("Recent evidence must be the latest questions")
        expected = "insufficient_evidence" if self.attempted < MIN_EVIDENCE else "observed"
        if self.evidence_status != expected:
            raise ValueError("Evidence status does not match the reporting threshold")
        if self.weakness_indicator and (
            self.recent_attempted < MIN_EVIDENCE or self.recent_marks_accuracy >= WEAKNESS_MARKS
        ):
            raise ValueError("Needs practice does not match the recent marks")
        return self


class StudentPerformance(Contract):
    student_id: str
    topics: list[TopicMetric]
    evidence_status: Literal["no_finalized_evidence", "available"]


class ClassPerformanceResponse(Contract):
    """one page of a classroom's enrolled students, in identity's roster order, each with their topic evidence."""

    classroom_id: UUID
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    students: list[StudentPerformance] = Field(max_length=50)
    interpretation: str


class PerformanceResponse(Contract):
    student_id: str
    classroom_id: UUID
    topics: list[TopicMetric]
    evidence_status: Literal["no_finalized_evidence", "available"]
    interpretation: str


def by_student(students: list[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """each student's topic summary, in the given order; a student without evidence has no topics."""
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        evidence[row["student_id"]].append(row)
    return [
        {
            "student_id": student,
            "topics": summarize(evidence[student]),
            "evidence_status": "available" if evidence[student] else "no_finalized_evidence",
        }
        for student in students
    ]


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """each topic's counts from evidence rows (topic_id, correct, awarded, possible), oldest first."""
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_topic[str(row["topic_id"])].append(row)
    return [_topic(topic, topic_rows) for topic, topic_rows in sorted(by_topic.items())]


def _topic(topic: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    correct = sum(int(r["correct"]) for r in rows)
    recent = rows[-RECENT_QUESTIONS:]
    recent_awarded = sum(int(r["awarded"]) for r in recent)
    recent_possible = sum(int(r["possible"]) for r in recent)
    return {
        "topic_id": topic,
        "attempted": n,
        "correct": correct,
        "observed_accuracy": round(correct / n, 4),
        "marks_awarded": sum(int(r["awarded"]) for r in rows),
        "marks_possible": sum(int(r["possible"]) for r in rows),
        "recent_attempted": len(recent),
        "recent_marks_accuracy": round(recent_awarded / recent_possible, 4),
        "evidence_status": "insufficient_evidence" if n < MIN_EVIDENCE else "observed",
        "weakness_indicator": len(recent) >= MIN_EVIDENCE
        and recent_awarded < WEAKNESS_MARKS * recent_possible,
    }
