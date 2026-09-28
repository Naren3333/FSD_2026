from typing import Literal
from uuid import UUID
from pydantic import Field
from learning_transport import Contract


class SourceReference(Contract):
    document_id: UUID
    revision: int = Field(ge=1)
    chunk_ordinal: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RequestContract(Contract):
    document_ids: list[UUID] = Field(min_length=1, max_length=20)
    skill_ids: list[UUID] = Field(min_length=1, max_length=20)
    classroom_id: UUID
    question_type: Literal["multiple_choice", "numerical"]
    difficulty: Literal["introductory", "intermediate", "advanced"]
    count: int = Field(ge=1, le=20)
    performance_context_id: UUID | None = None


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
