from uuid import UUID
from pydantic import Field, model_validator
from learning_transport import Contract


class SourceReference(Contract):
    document_id: UUID
    revision: int = Field(ge=1)
    chunk_ordinal: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SkillSummary(Contract):
    skill_id: UUID
    attempted: int = Field(ge=0)
    correct: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self) -> "SkillSummary":
        if self.correct > self.attempted:
            raise ValueError("Correct count exceeds attempts")
        return self


class RequestContract(Contract):
    student_id: str = Field(min_length=1, max_length=200)
    classroom_id: UUID
    performance_summary: list[SkillSummary] = Field(min_length=1, max_length=100)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=100)
    skill_ids: list[UUID] = Field(min_length=1, max_length=100)


class Observation(Contract):
    text: str
    evidence_ids: list[UUID]


class ResponseContract(Contract):
    observations: list[Observation]
    possible_misconceptions: list[Observation]
    suggested_interventions: list[str]
    recommended_skill_ids: list[UUID]
