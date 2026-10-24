"""the question-generation agent's http contract (adr 0006)."""

from collections.abc import Sequence
from typing import Annotated, Literal, Self, get_args
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
TYPE_ORDER = get_args(Generatable)
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
    counts: dict[Generatable, int]  # how many of each type to write
    difficulty: Difficulty
    focus: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_FOCUS)] = ""
    # the questions the editor already holds, so the agent can drop near-duplicates.
    existing_prompts: list[Annotated[str, StringConstraints(max_length=MAX_PROMPT)]] = Field(
        default=[], max_length=MAX_EXISTING
    )
    review: bool = True  # false skips the reviewer pass for this request

    @model_validator(mode="after")
    def distinct(self) -> Self:
        _distinct(self.document_ids, "document_ids")
        if not self.counts:
            raise ValueError("Choose at least one question type.")
        if any(not 1 <= n <= MAX_COUNT for n in self.counts.values()):
            raise ValueError(f"Ask for 1 to {MAX_COUNT} questions of each type.")
        if sum(self.counts.values()) > MAX_COUNT:
            raise ValueError(f"Ask for at most {MAX_COUNT} questions in total.")
        return self

    @property
    def question_types(self) -> list[Generatable]:
        return [t for t in TYPE_ORDER if t in self.counts]

    @property
    def count(self) -> int:
        return sum(self.counts.values())


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


ReviewStatus = Literal["passed", "flagged", "not_reviewed"]


class Review(Contract):
    status: ReviewStatus = "not_reviewed"
    note: str = ""  # the reviewer's note for flagged; why review did not run for not_reviewed; "" for passed
    round: int = 0  # 0 = first attempt, 1-2 = written in a retry
    source: Literal["reviewer", "check"] = "reviewer"  # who wrote a flagged note: the model or our own code


class Generated(Contract):
    question: DraftQuestion
    evidence: str  # quoted from a source passage; it supports the answer key
    explanation: str  # why the key is right, for the teacher
    review: Review = Field(default_factory=Review)


class Round(Contract):
    generated: int  # questions in the model's reply
    passed_checks: int
    passed_review: int  # equals passed_checks when the round was not reviewed
    rejected: dict[str, int]  # by reason, from checks and reviewer, this round only
    reviewed: bool = False  # the review call returned verdicts for this round
    retried: bool = False  # a later round ran after this one
    sent_back: dict[str, int] = Field(default_factory=dict)  # by reason, the drafts fed back to the generator


class GenerateResponse(Contract):
    questions: list[Generated]
    requested: int
    requested_by_type: dict[str, int]
    returned: int
    dropped: dict[str, int]  # questions the checks or the reviewer refused, by reason
    rounds: list[Round] = Field(default_factory=list)
    model_calls: int = 0
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
