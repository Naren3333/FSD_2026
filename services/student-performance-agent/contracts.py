from typing import Literal
from uuid import UUID

from learning_transport import Contract
from pydantic import Field, model_validator


class SkillSummary(Contract):
    """A skill's finalized evidence as Analytics reports it (ADR 0004): questions attempted and answered with full
    marks, and the points earned out of the points possible, which show part marks."""

    skill_id: UUID
    attempted: int = Field(ge=0)
    correct: int = Field(ge=0)
    marks_awarded: int = Field(ge=0)
    marks_possible: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self) -> "SkillSummary":
        if self.correct > self.attempted:
            raise ValueError("Correct count exceeds attempts")
        if self.marks_awarded > self.marks_possible or self.marks_possible < self.attempted:
            raise ValueError("Marks must match the attempted questions")
        return self


class Billed(Contract):
    """Whose daily AI allowance would pay for the interpretation (ADR 0003), taken from the user's verified token."""

    user_id: str = Field(min_length=1, max_length=200)
    org_id: str = Field(min_length=1, max_length=200)
    role: Literal["student", "teacher"]


class RequestContract(Contract):
    """Not authorized: always 501."""

    student_id: str = Field(min_length=1, max_length=200)
    classroom_id: UUID
    performance_summary: list[SkillSummary] = Field(min_length=1, max_length=100)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=100)
    skill_ids: list[UUID] = Field(min_length=1, max_length=100)
    billed_to: Billed


class Observation(Contract):
    text: str
    evidence_ids: list[UUID]


class ResponseContract(Contract):
    observations: list[Observation]
    possible_misconceptions: list[Observation]
    suggested_interventions: list[str]
    recommended_skill_ids: list[UUID]
