"""service-local views of the upstream fields this agent uses."""

from typing import Annotated, Literal, NotRequired

from learning_transport import UpstreamError
from pydantic import Field, TypeAdapter, ValidationError
from typing_extensions import TypedDict

Count = Annotated[int, Field(ge=0)]
Accuracy = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class TopicMetrics(TypedDict):
    topic_id: str
    attempted: Count
    correct: Count
    marks_awarded: Count
    marks_possible: Count
    observed_accuracy: Accuracy
    evidence_status: str
    weakness_indicator: bool
    recent_marks_accuracy: NotRequired[Accuracy | None]


class Performance(TypedDict):
    topics: list[TopicMetrics]


class ClassTopic(TypedDict):
    topic_id: str
    attempted: Count
    correct: Count


class ClassStudent(TypedDict):
    student_id: str
    topics: list[ClassTopic]


class ClassPage(TypedDict):
    students: list[ClassStudent]
    total: Count


class CriterionScore(TypedDict):
    criterion_id: str
    points: Count


class Evidence(TypedDict):
    question_id: str
    topic_id: str
    awarded: Count
    possible: Annotated[int, Field(ge=1)]
    feedback: NotRequired[str | None]
    criteria: NotRequired[list[CriterionScore] | None]


class Evaluation(TypedDict):
    id: str
    assessment_id: str
    status: Literal["draft", "finalized"]
    evidence: list[Evidence]
    answers: NotRequired[dict[str, str] | None]
    finalized_at: NotRequired[str | None]
    released_at: NotRequired[str | None]
    late: NotRequired[bool]


class EvaluationPage(TypedDict):
    evaluations: list[Evaluation]
    total: Count


class Level(TypedDict):
    points: Count


class Criterion(TypedDict):
    id: str
    title: str
    levels: list[Level]


class Rubric(TypedDict):
    model_answer: NotRequired[str]
    criteria: list[Criterion]


class TeacherQuestion(TypedDict):
    id: str
    prompt: str
    type: str
    options: NotRequired[list[str] | None]
    answer: NotRequired[str]
    accepted_answers: NotRequired[list[str] | None]
    rubric: NotRequired[Rubric | None]


class Assessment(TypedDict):
    id: str
    title: str
    questions: list[TeacherQuestion]


class TopicName(TypedDict):
    id: str
    name: str


performance = TypeAdapter(Performance)
class_page = TypeAdapter(ClassPage)
evaluation_page = TypeAdapter(EvaluationPage)
assessments = TypeAdapter(list[Assessment])
assessment = TypeAdapter(Assessment)
topic_names = TypeAdapter(list[TopicName])


def read[T](adapter: TypeAdapter[T], value: object, service: str) -> T:
    try:
        return adapter.validate_python(value, strict=True)
    except ValidationError:
        raise UpstreamError(service, 502) from None
