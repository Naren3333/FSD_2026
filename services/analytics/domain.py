from collections import defaultdict
from typing import Any, Literal
from uuid import UUID

from learning_transport import Contract
from learning_transport.kafka import Envelope
from pydantic import Field, model_validator


class Evidence(Contract):
    question_id: UUID
    skill_id: UUID
    correct: bool
    awarded: int = Field(ge=0, le=1, strict=True)
    possible: int = Field(ge=1, le=1, strict=True)

    @model_validator(mode="after")
    def consistent(self) -> "Evidence":
        if self.correct != (self.awarded == self.possible):
            raise ValueError("Correctness and awarded score disagree")
        return self


class Finalized(Contract):
    evaluation_id: UUID
    assessment_id: UUID
    classroom_id: UUID
    org_id: str = Field(min_length=1, max_length=200)
    student_id: str = Field(min_length=1, max_length=200)
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
        if self.event_type != "grading.finalized.v1" or self.resource_id != self.data.evaluation_id:
            raise ValueError("Not a finalized grading event")
        return self


class ClassroomDeleted(Contract):
    classroom_id: UUID
    org_id: str = Field(min_length=1, max_length=200)


class ClassroomDeletedEvent(Envelope):
    event_type: Literal["identity.classroom.deleted.v1"]
    data: ClassroomDeleted

    @model_validator(mode="after")
    def deleted(self) -> "ClassroomDeletedEvent":
        if self.resource_id != self.data.classroom_id:
            raise ValueError("Not this classroom's deletion")
        return self


class SkillMetric(Contract):
    skill_id: UUID
    attempted: int = Field(ge=1)
    correct: int = Field(ge=0)
    observed_accuracy: float = Field(ge=0, le=1)
    evidence_status: Literal["insufficient_evidence", "observed"]
    weakness_indicator: bool

    @model_validator(mode="after")
    def evidence_matches(self) -> "SkillMetric":
        if self.correct > self.attempted or self.observed_accuracy != round(self.correct / self.attempted, 4):
            raise ValueError("Accuracy must match the observed counts")
        expected = "insufficient_evidence" if self.attempted < 5 else "observed"
        if self.evidence_status != expected:
            raise ValueError("Evidence status does not match the reporting threshold")
        return self


class StudentPerformance(Contract):
    student_id: str
    skills: list[SkillMetric]
    evidence_status: Literal["no_finalized_evidence", "available"]


class ClassPerformanceResponse(Contract):
    """One page of a classroom's enrolled students, in Identity's roster order, each with their skill evidence."""

    classroom_id: UUID
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    students: list[StudentPerformance] = Field(max_length=50)
    interpretation: str


class PerformanceResponse(Contract):
    student_id: str
    classroom_id: UUID
    skills: list[SkillMetric]
    evidence_status: Literal["no_finalized_evidence", "available"]
    interpretation: str


def by_student(students: list[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each student's skill summary, in the given order; a student without evidence has no skills."""
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        evidence[row["student_id"]].append(row)
    return [
        {
            "student_id": student,
            "skills": summarize(evidence[student]),
            "evidence_status": "available" if evidence[student] else "no_finalized_evidence",
        }
        for student in students
    ]


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        skill = str(row["skill_id"])
        counts[skill][0] += 1
        counts[skill][1] += int(row["correct"])
    return [
        {
            "skill_id": skill,
            "attempted": n,
            "correct": correct,
            "observed_accuracy": round(correct / n, 4),
            "evidence_status": "insufficient_evidence" if n < 5 else "observed",
            "weakness_indicator": n >= 5 and correct / n < 0.6,
        }
        for skill, (n, correct) in sorted(counts.items())
    ]
