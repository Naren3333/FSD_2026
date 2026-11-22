"""content fields used by generation, validated at the service boundary."""

from typing import Annotated, NotRequired

import httpx
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


class ListedDocument(TypedDict):
    id: str
    title: str


class DocumentPage(TypedDict):
    items: list[ListedDocument]
    total: int


document_page = TypeAdapter(DocumentPage)


def read_titles(resp: httpx.Response) -> tuple[dict[str, str], int]:
    """one page of content's document list as id → title, and the list's total; extra fields are ignored."""
    try:
        page = document_page.validate_python(resp.json())
    except (ValueError, ValidationError):
        raise UpstreamError("content", 502) from None
    return {d["id"]: d["title"] for d in page["items"]}, page["total"]
