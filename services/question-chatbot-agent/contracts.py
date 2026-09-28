from uuid import UUID
from pydantic import Field
from learning_transport import Contract


class SourceReference(Contract):
    document_id: UUID
    revision: int = Field(ge=1)
    chunk_ordinal: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RequestContract(Contract):
    student_id: str = Field(min_length=1, max_length=200)
    classroom_id: UUID
    document_ids: list[UUID] = Field(min_length=1, max_length=20)
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID
    retrieved_evidence: list[SourceReference] = Field(max_length=30)


class ResponseContract(Contract):
    answer: str | None
    citations: list[SourceReference]
    insufficient_evidence: bool
