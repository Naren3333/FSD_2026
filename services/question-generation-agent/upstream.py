"""content fields used by generation, validated at the service boundary."""

from typing import Annotated, NotRequired

from learning_transport import UpstreamError
from pydantic import Field, TypeAdapter, ValidationError
from typing_extensions import TypedDict


class SearchPassage(TypedDict):
    chunk_id: str
    document_id: str
    content: str
    score: Annotated[float, Field(allow_inf_nan=False)]
    title: NotRequired[str | None]
    page_start: NotRequired[Annotated[int, Field(ge=1)] | None]
    section_path: NotRequired[str | None]
    source: NotRequired[str]


passages = TypeAdapter(list[SearchPassage])


def read_passages(value: object) -> list[SearchPassage]:
    try:
        return passages.validate_python(value, strict=True)
    except ValidationError:
        raise UpstreamError("content", 502) from None
