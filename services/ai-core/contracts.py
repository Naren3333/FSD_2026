from typing import Literal, Protocol
from uuid import UUID
from pydantic import Field
from learning_transport import Contract


class SourceReference(Contract):
    document_id: UUID
    revision: int = Field(ge=1)
    chunk_ordinal: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RequestContract(Contract):
    operation: Literal["generate", "embed"]
    input: str = Field(min_length=1, max_length=20000)
    model_profile: str = Field(min_length=1, max_length=100)


class ResponseContract(Contract):
    output: str | None = None
    vectors: list[list[float]] | None = None
    model_version: str
    usage_tokens: int = Field(ge=0)


class InferenceProvider(Protocol):
    async def execute(self, request: RequestContract) -> ResponseContract: ...
