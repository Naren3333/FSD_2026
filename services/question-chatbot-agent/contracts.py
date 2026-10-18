from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from learning_transport import Contract
from pydantic import Field, StringConstraints

# Content caps a search query at 1000 characters, and the raw message is the query when the rewrite fails.
MAX_MESSAGE = 1000
MAX_MESSAGES = 200  # the most messages one read of a thread returns: its latest, oldest first


class NewThread(Contract):
    classroom_id: UUID


class Thread(Contract):
    id: UUID
    classroom_id: UUID
    role: Literal["student", "teacher"]
    title: str | None
    created_at: datetime
    last_message_at: datetime


class ThreadList(Contract):
    threads: list[Thread]


class NewMessage(Contract):
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE)]


class Citation(Contract):
    """A chunk the answer cites; `[doc:<chunk_id>]` in the message text marks where."""

    chunk_id: UUID
    document_id: UUID
    title: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    kind: Literal["text", "table", "figure"]
    source: Literal["pdf", "vision"]
    content: str
    image: str | None  # GET /api/content/documents/{document_id}/figures/{image}
    # The teacher deleted the document: its passage and figure are gone (content and image are empty).
    removed: bool = False


class Message(Contract):
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    outcome: Literal["answered", "no_results", "error", "interrupted"] | None
    citations: list[Citation] | None
    created_at: datetime


class MessageList(Contract):
    """A thread's latest messages, oldest first."""

    messages: list[Message] = Field(max_length=MAX_MESSAGES)


class Usage(Contract):
    """The caller's AI spend today (chat, search and, for teachers, indexing their uploads), their daily limit
    and when the day resets."""

    spent_usd: float = Field(ge=0)
    limit_usd: float = Field(gt=0)
    resets_at: datetime
