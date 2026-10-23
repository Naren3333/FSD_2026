"""the performance interpretation agent's http contract (adr 0014)."""

from typing import Literal
from uuid import UUID

from learning_transport import Contract
from pydantic import Field


class RequestContract(Contract):
    """the student to interpret, in a classroom the caller teaches; the agent gathers the evidence itself."""

    classroom_id: UUID
    student_id: str = Field(min_length=1, max_length=200)


class Observation(Contract):
    """a statement about the student and the questions (their finalized marks) it rests on."""

    text: str
    evidence_ids: list[UUID] = Field(min_length=1)


class Evidence(Contract):
    """a finalized question result an observation cites, for the teacher to check."""

    question_id: UUID
    evaluation_id: UUID
    assessment_id: UUID
    assessment_title: str
    topic_id: UUID
    prompt: str
    awarded: int = Field(ge=0)
    possible: int = Field(ge=1)


class Step(Contract):
    """one tool call the agent chose, in order, and what came of it."""

    tool: str
    reason: str
    target: str  # what it looked at, in words: an assessment's title, a topic's name, or "whole class"
    outcome: Literal["ok", "refused", "failed"]


class ResponseContract(Contract):
    """a draft interpretation for the classroom's teacher; it never changes a grade."""

    classroom_id: UUID
    student_id: str
    observations: list[Observation]
    possible_misconceptions: list[Observation]
    suggested_interventions: list[str]
    recommended_topic_ids: list[UUID]
    evidence: list[Evidence]
    steps: list[Step]
    model_turns: int = Field(ge=1)
    # what the checks removed from the model's report, by reason.
    dropped: dict[str, int]
    requires_teacher_review: Literal[True] = True
