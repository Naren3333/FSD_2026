from typing import Literal
from uuid import UUID

from learning_transport import Contract
from pydantic import Field


class SourceReference(Contract):
    document_id: UUID
    revision: int = Field(ge=1)
    chunk_ordinal: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Billed(Contract):
    """Whose daily AI allowance would pay for the generation (ADR 0003), taken from the user's verified token."""

    user_id: str = Field(min_length=1, max_length=200)
    org_id: str = Field(min_length=1, max_length=200)
    role: Literal["student", "teacher"]


class RequestContract(Contract):
    """ADR 0004's question types, each question worth 1 to 100 points (a free response's points are its rubric's
    top levels), and the user the model calls would be billed to. Not authorized: always 501."""

    document_ids: list[UUID] = Field(min_length=1, max_length=20)
    skill_ids: list[UUID] = Field(min_length=1, max_length=20)
    classroom_id: UUID
    question_type: Literal["multiple_choice", "numerical", "short_answer", "free_response"]
    points: int = Field(ge=1, le=100)
    difficulty: Literal["introductory", "intermediate", "advanced"]
    count: int = Field(ge=1, le=20)
    performance_context_id: UUID | None = None
    billed_to: Billed


class GeneratedQuestion(Contract):
    prompt: str
    answer_key: str
    explanation: str
    source_references: list[SourceReference]
    skill_ids: list[UUID]
    validation_metadata: dict[str, str]


class ResponseContract(Contract):
    questions: list[GeneratedQuestion]
    requires_teacher_review: Literal[True]
