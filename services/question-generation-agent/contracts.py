"""the question-generation agent's http contract (adr 0006)."""

from collections.abc import Sequence
from typing import Annotated, Literal, Self
from uuid import UUID

from learning_transport import Contract
from pydantic import Field, StringConstraints, model_validator

# the limits assessment applies (services/assessment/questions.go and domain.go).
MAX_PROMPT = 2000
MAX_ANSWER = 500  # also an option and an accepted answer
MAX_ACCEPTED = 20
MIN_OPTIONS, MAX_OPTIONS = 2, 8
MAX_CRITERIA = 10
MIN_LEVELS, MAX_LEVELS = 2, 6
MAX_CRITERION = 200
MAX_DESCRIPTOR = 500
MAX_MODEL_ANSWER = 5000
MAX_POINTS = 100
MAX_EXCERPT_TEXT = 2000
MAX_SOURCES = 5
DEFAULT_MAX_LENGTH = 5000

# this agent's own limits.
MAX_COUNT = 10
MAX_DOCUMENTS = 10
MAX_FOCUS = 300
MAX_EXISTING = 50

Generatable = Literal["multiple_choice", "short_answer", "free_response"]
TeacherMarked = Literal["short_answer", "free_response"]
Difficulty = Literal["introductory", "intermediate", "advanced"]
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PROMPT)]


def _distinct(values: Sequence[object], what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must not repeat")


class GenerateRequest(Contract):
    classroom_id: UUID
    document_ids: list[UUID] = Field(min_length=1, max_length=MAX_DOCUMENTS)
    topic_id: UUID
    question_types: list[Generatable] = Field(min_length=1, max_length=3)
    difficulty: Difficulty
    count: int = Field(ge=1, le=MAX_COUNT)
    focus: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_FOCUS)] = ""
    # the questions the editor already holds, so the agent can drop near-duplicates.
    existing_prompts: list[Annotated[str, StringConstraints(max_length=MAX_PROMPT)]] = Field(
        default=[], max_length=MAX_EXISTING
    )

    @model_validator(mode="after")
    def distinct(self) -> Self:
        _distinct(self.document_ids, "document_ids")
        _distinct(self.question_types, "question_types")
        return self


class Excerpt(Contract):
    """a passage, as assessment stores it: from a document (with its ids and page), or written with no ids."""

    document_id: UUID | None = None
    chunk_id: UUID | None = None
    page: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=MAX_EXCERPT_TEXT)


class Level(Contract):
    points: int = Field(ge=0, le=MAX_POINTS)
    descriptor: str


class Criterion(Contract):
    title: str
    levels: list[Level]


class Rubric(Contract):
    criteria: list[Criterion]
    model_answer: str


class DraftQuestion(Contract):
    """a question as assessment takes it in a draft, with its sources and origin (adr 0006)."""

    prompt: str
    type: Generatable
    topic_id: UUID
    points: int = Field(ge=1, le=MAX_POINTS)
    options: list[str]
    answer: str
    tolerance: Literal[""] = ""
    accepted_answers: list[str] = Field(default_factory=list)
    case_sensitive: bool = False
    rubric: Rubric | None = None
    max_length: int | None = None
    marking_scheme: list[Excerpt] = Field(default_factory=list)
    sources: list[Excerpt] = Field(min_length=1, max_length=MAX_SOURCES)
    origin: Literal["generated"] = "generated"


class Generated(Contract):
    question: DraftQuestion
    evidence: str  # quoted from a source passage; it supports the answer key
    explanation: str  # why the key is right, for the teacher


class GenerateResponse(Contract):
    questions: list[Generated]
    requested: int
    returned: int
    dropped: dict[str, int]  # questions the checks refused, by reason
    requires_teacher_review: Literal[True] = True


class GuidanceRequest(Contract):
    """marking guidance for a question the teacher wrote, drafted from the documents they choose."""

    classroom_id: UUID
    document_ids: list[UUID] = Field(min_length=1, max_length=MAX_DOCUMENTS)
    prompt: Prompt
    type: TeacherMarked
    points: int = Field(default=1, ge=1, le=MAX_POINTS)  # a short answer's marks

    @model_validator(mode="after")
    def distinct(self) -> Self:
        _distinct(self.document_ids, "document_ids")
        return self


class Guidance(Contract):
    type: TeacherMarked
    accepted_answers: list[str]  # a short answer's suggested accepted answers; empty for a free response
    rubric: Rubric | None  # a free response's rubric and model answer; none for a short answer
    marking_points: Excerpt  # what earns each mark, as a marking-scheme passage with no document
    sources: list[Excerpt]
    evidence: str
    requires_teacher_review: Literal[True] = True
