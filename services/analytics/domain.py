from collections import defaultdict
from typing import Any, Literal
from uuid import UUID

from learning_transport import Contract
from learning_transport.kafka import Envelope
from pydantic import Field, model_validator

FINALIZED = "grading.finalized.v3"


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
    """a topic's finalized evidence: questions attempted and answered with full marks, and the marks earned out of the marks possible (which shows part marks)."""

    topic_id: UUID
    attempted: int = Field(ge=1)
    correct: int = Field(ge=0)
    observed_accuracy: float = Field(ge=0, le=1)
    marks_awarded: int = Field(ge=0)
    marks_possible: int = Field(ge=1)
    evidence_status: Literal["insufficient_evidence", "observed"]
    weakness_indicator: bool

    @model_validator(mode="after")
    def evidence_matches(self) -> "TopicMetric":
        if self.correct > self.attempted or self.observed_accuracy != round(self.correct / self.attempted, 4):
            raise ValueError("Accuracy must match the observed counts")
        if self.marks_awarded > self.marks_possible or self.marks_possible < self.attempted:
            raise ValueError("Marks must match the observed questions")
        expected = "insufficient_evidence" if self.attempted < 5 else "observed"
        if self.evidence_status != expected:
            raise ValueError("Evidence status does not match the reporting threshold")
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
    """each topic's counts from evidence rows (topic_id, correct, awarded, possible)."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for row in rows:
        topic = counts[str(row["topic_id"])]
        topic[0] += 1
        topic[1] += int(row["correct"])
        topic[2] += int(row["awarded"])
        topic[3] += int(row["possible"])
    return [
        {
            "topic_id": topic,
            "attempted": n,
            "correct": correct,
            "observed_accuracy": round(correct / n, 4),
            "marks_awarded": awarded,
            "marks_possible": possible,
            "evidence_status": "insufficient_evidence" if n < 5 else "observed",
            "weakness_indicator": n >= 5 and correct / n < 0.6,
        }
        for topic, (n, correct, awarded, possible) in sorted(counts.items())
    ]
