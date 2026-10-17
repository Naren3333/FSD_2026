import json
from typing import Any, Literal, Self

from learning_transport import Contract
from pydantic import Field, model_validator

from spend import Billed

MAX_SCHEMA_CHARS = 20000  # a response_schema, as JSON: the callers' schemas are a few hundred characters


class Message(Contract):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=60000)


class RequestContract(Contract):
    """`embed` takes `input`. `generate` takes `input` (one user message) or `messages`; with `response_schema`
    the reply is JSON in that strict schema, and with `stream` it arrives as server-sent events."""

    operation: Literal["generate", "embed"]
    input: str | None = Field(default=None, min_length=1, max_length=20000)
    messages: list[Message] | None = Field(default=None, min_length=1, max_length=50)
    max_tokens: int = Field(default=1000, ge=1, le=8000)
    response_schema: dict[str, Any] | None = None
    schema_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    stream: bool = False
    model_profile: Literal["default"] = "default"
    billed_to: Billed  # whose daily AI allowance pays for the call (ADR 0003)

    @model_validator(mode="after")
    def one_input(self) -> Self:
        if self.operation == "embed":
            if (
                self.input is None
                or self.messages is not None
                or self.response_schema is not None
                or self.stream
            ):
                raise ValueError("embed takes input only")
        elif (self.input is None) == (self.messages is None):
            raise ValueError("generate takes input or messages")
        elif self.stream and self.response_schema is not None:
            raise ValueError("a structured reply cannot be streamed")
        if self.response_schema is not None and len(json.dumps(self.response_schema)) > MAX_SCHEMA_CHARS:
            raise ValueError(f"response_schema is over {MAX_SCHEMA_CHARS} characters as JSON")
        return self


class ResponseContract(Contract):
    output: str | None = None
    vectors: list[list[float]] | None = None
    model_version: str
    usage_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None


class Passage(Contract):
    """A search hit as Content stores it. ai-core builds the text the reranker scores from these parts
    (ingest.passage_text, the same rule that built the embedded text), so the rule lives in one place."""

    title: str = Field(min_length=1, max_length=200)
    section_path: str | None = Field(default=None, max_length=20000)
    content: str = Field(min_length=1, max_length=20000)


class RerankRequest(Contract):
    query: str = Field(min_length=1, max_length=2000)
    passages: list[Passage] = Field(min_length=1, max_length=100)
    billed_to: Billed


class RerankResponse(Contract):
    scores: list[float]  # in the passages' order
    model_version: str
    usage_tokens: int | None = Field(default=None, ge=0)


class IngestChunk(Contract):
    ordinal: int = Field(ge=0)
    content: str
    embed_text: str
    section_path: str | None
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    token_count: int = Field(ge=0)
    kind: Literal["text", "table", "figure"]
    source: Literal["pdf", "vision"]
    image: str | None  # a figure's crop, one of the response's figures
    label: str | None
    cites: list[str]
    embedding: list[float]


class IngestFigure(Contract):
    image: str = Field(pattern=r"^[a-f0-9]{16}\.png$")
    png_base64: str


class IngestResponse(Contract):
    """`failed` is the document's own problem (ENCRYPTED, CORRUPT, NO_TEXT_LAYER) and retrying will not help.
    Provider outages are errors instead, for the caller to retry."""

    outcome: Literal["indexed", "failed"]
    failure_reason: Literal["NO_TEXT_LAYER", "ENCRYPTED", "CORRUPT"] | None = None
    failure_detail: str | None = None
    page_count: int | None = Field(default=None, ge=0)
    ocr_page_count: int | None = Field(default=None, ge=0)
    embedding_model: str | None = None
    chunks: list[IngestChunk] = Field(default_factory=list)
    figures: list[IngestFigure] = Field(default_factory=list)
