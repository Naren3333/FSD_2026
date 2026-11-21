"""the student performance agent's http contracts: performance interpretation (adr 0014) and class insights (adr 0021)."""

from typing import Annotated, Literal
from uuid import UUID

from learning_transport import Contract
from pydantic import Field, StringConstraints


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


class ClassComparisonScope(Contract):
    population: int = Field(ge=0)
    compared: int = Field(ge=0)
    partial: bool
    selection: Literal["roster_order"] = "roster_order"


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
    class_comparison_scope: ClassComparisonScope | None = None
    requires_teacher_review: Literal[True] = True


# ---- class insights (adr 0021)

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ClassInsightsRequest(Contract):
    """a teacher's question about a classroom they teach; earlier questions are context, never evidence."""

    classroom_id: UUID
    question: Question
    previous_questions: list[Question] = Field(default_factory=list, max_length=3)


class ClassEvidence(Evidence):
    """one student's finalized question result a statement cites; `evidence_id` is local to this response."""

    evidence_id: str
    student_id: str


class StrugglingGroup(Contract):
    """students whose released evidence on a topic shows they need practice."""

    topic_id: UUID
    student_ids: list[str] = Field(min_length=1)
    text: str


class ClassObservation(Contract):
    """a statement about the class, with the topics, students and question results it rests on."""

    text: str
    topic_ids: list[UUID]
    student_ids: list[str]
    evidence_ids: list[str]


class ClassMisconception(Contract):
    """a possible wrong idea that answers the agent read show."""

    text: str
    evidence_ids: list[str] = Field(min_length=1)


class Reteach(Contract):
    """a topic to teach again, and the question results that show why."""

    topic_id: UUID
    text: str
    evidence_ids: list[str]


class CitedStudent(Contract):
    """a student the report names, for the teacher only: the model saw references, never names."""

    student_id: str
    display_name: str


class ClassInsightsResponse(Contract):
    """a draft answer for the classroom's teacher; nothing is saved and no grade changes."""

    classroom_id: UUID
    question: str
    struggling: list[StrugglingGroup]
    observations: list[ClassObservation]
    possible_misconceptions: list[ClassMisconception]
    reteach: list[Reteach]
    students: list[CitedStudent]
    evidence: list[ClassEvidence]
    steps: list[Step]
    model_turns: int = Field(ge=1)
    # what the checks removed from the model's report, by reason.
    dropped: dict[str, int]
    scope: ClassComparisonScope
    requires_teacher_review: Literal[True] = True
