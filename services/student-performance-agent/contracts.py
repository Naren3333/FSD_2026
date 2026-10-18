from uuid import UUID

from learning_transport import Contract
from pydantic import Field, model_validator


class TopicSummary(Contract):
    topic_id: UUID
    attempted: int = Field(ge=0)
    correct: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self) -> "TopicSummary":
        if self.correct > self.attempted:
            raise ValueError("Correct count exceeds attempts")
        return self


class RequestContract(Contract):
    student_id: str = Field(min_length=1, max_length=200)
    classroom_id: UUID
    performance_summary: list[TopicSummary] = Field(min_length=1, max_length=100)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=100)
    topic_ids: list[UUID] = Field(min_length=1, max_length=100)


class Observation(Contract):
    text: str
    evidence_ids: list[UUID]


class ResponseContract(Contract):
    observations: list[Observation]
    possible_misconceptions: list[Observation]
    suggested_interventions: list[str]
    recommended_topic_ids: list[UUID]
