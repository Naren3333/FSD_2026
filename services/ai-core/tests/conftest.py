import array
import base64
import json
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pymupdf
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from learning_transport.testing import as_caller, token_claims
from openai import AsyncOpenAI

import main
import openai_client
import rerank
import settings
import spend
from ingest import VisionConfig
from main import app

for name, value in {
    "OIDC_ISSUER": "https://issuer.test",
    "OIDC_AUDIENCE": "learning",
    "OIDC_JWKS_URL": "https://issuer.test/certs",
}.items():
    os.environ.setdefault(name, value)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def configured(**overrides: Any) -> settings.Settings:
    """Settings as from an empty environment, with test keys: never the developer's real ones."""
    return replace(
        settings.Settings(**{**_EMPTY, "openai_api_key": "sk-test", "voyage_api_key": "pa-test"}), **overrides
    )


@pytest.fixture(autouse=True)
def test_settings(monkeypatch: pytest.MonkeyPatch) -> settings.Settings:
    """Every module sees test settings: keys set, vision on, the cache in a fresh temp folder."""
    current = configured(cache_dir=Path(tempfile.mkdtemp(prefix="ai-core-cache-")))
    for module in ("settings", "openai_client", "rerank", "ingest", "main", "spend", "billing"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: current)
    return current


SERVICE_CLAIMS = token_claims("ai-client", sub="content-indexer")
BILLED = {"user_id": "student-1", "org_id": "test-org", "role": "student"}
BILLED_PARAMS = {"billed_user": "teacher-1", "billed_org": "test-org", "billed_role": "teacher"}


class MemoryLedger:
    """Test double for the Postgres usage ledger: charges kept in a list, a fixed reset time, and `already`
    pre-spent dollars per user. Never wired in the application."""

    def __init__(self) -> None:
        self.charges: list[tuple[spend.Billed, spend.Charge]] = []
        self.already: dict[str, float] = {}
        self.broken = False

    async def today(self, billed: spend.Billed) -> spend.Usage:
        if self.broken:
            raise ConnectionError("ledger down")
        spent = self.already.get(billed.user_id, 0.0) + sum(
            c.cost_usd for b, c in self.charges if b.user_id == billed.user_id
        )
        return spend.Usage(
            spent_usd=spent, limit_usd=spend.limit(billed.role), resets_at=datetime(2026, 10, 14, tzinfo=UTC)
        )

    async def record(self, billed: spend.Billed, charge: spend.Charge, correlation_id: str) -> None:
        self.charges.append((billed, charge))

    async def ping(self) -> None:
        if self.broken:
            raise ConnectionError("ledger down")

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def ledger() -> Iterator[MemoryLedger]:
    """A fresh in-memory ledger for every test; the lifespan keeps a ledger it finds instead of opening Postgres."""
    app.state.ledger = fake = MemoryLedger()
    yield fake
    del app.state.ledger


@pytest.fixture
def api() -> Iterator[TestClient]:
    """The app as a service account holding ai-client. The override is a test double for verified OIDC claims,
    never wired in the application."""
    with as_caller(app, SERVICE_CLAIMS), TestClient(app) as client:
        yield client


def make_pdf(
    pages: int = 1,
    text_: str = "Photosynthesis turns light into chemical energy.",
    password: str | None = None,
    paragraphs: list[list[str]] | None = None,
) -> bytes:
    """A small text PDF built with PyMuPDF. Each page's text includes its number.
    With `paragraphs`, page i holds paragraphs[i], one text block each, instead."""
    doc = pymupdf.open()
    for i in range(len(paragraphs) if paragraphs is not None else pages):
        page = doc.new_page()
        if paragraphs is None:
            page.insert_text((72, 72), f"{text_} (page {i + 1})")
            continue
        y = 50
        for para in paragraphs[i]:
            height = 12 * (len(para) // 85 + 2)
            assert page.insert_textbox(pymupdf.Rect(50, y, 545, y + height), para, fontsize=9) >= 0, (
                "overflow"
            )
            y += height + 6
    if password:
        out = doc.tobytes(encryption=pymupdf.mupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    else:
        out = doc.tobytes()
    doc.close()
    return out


STYLES = {"h1": (18, "hebo"), "h2": (14, "hebo"), "h3": (10, "hebo"), "p": (10, "helv")}


def make_structured_pdf(
    elements: list[tuple[str, str]], header: str | None = None, footer: str | None = None
) -> bytes:
    """Headings and paragraphs flowed over as many pages as needed. `elements` are ("h1"|"h2"|"h3"|"p",
    text) or ("page", "") to start a new page; "#" in `header`/`footer` becomes the page number. Each
    element is its own text block."""
    doc = pymupdf.open()
    page, y = None, 0.0
    for kind, content in elements:
        if kind == "page":
            page = None
            continue
        size, font = STYLES[kind]
        height = size * 1.5 * (len(content) * size // 900 + 2)
        if page is None or y + height > 760:
            page, y = doc.new_page(), 70.0
            number = str(doc.page_count)
            if header:
                page.insert_text((50, 40), header.replace("#", number), fontsize=8)
            if footer:
                page.insert_text((50, 815), footer.replace("#", number), fontsize=8)
        assert (
            page.insert_textbox(pymupdf.Rect(50, y, 545, y + height), content, fontsize=size, fontname=font)
            >= 0
        ), "overflow"
        y += height + 6
    out = doc.tobytes()
    doc.close()
    return out


def make_table_pdf(
    header: list[str],
    rows: list[list[str]],
    *,
    ruled: bool = True,
    caption: str | None = None,
    before: str | None = None,
    after: str | None = None,
    widths: list[float] | None = None,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """One page: an optional paragraph, an optional caption line, a table (cell grid lines when `ruled`,
    otherwise whitespace only) and an optional paragraph after it. Returns the PDF and the table's box."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60.0
    if before:
        page.insert_textbox(pymupdf.Rect(50, y, 545, y + 60), before, fontsize=10)
        y += 70
    if caption:
        page.insert_text((60, y + 10), caption, fontsize=10)
        y += 20
    widths = widths or [470 / len(header)] * len(header)
    xs = [60.0]
    for w in widths:
        xs.append(xs[-1] + w)
    row_h, top = 20.0, y
    for r, cells in enumerate([header, *rows]):
        for c, cell in enumerate(cells):
            page.insert_text(
                (xs[c] + 4, top + r * row_h + 14), cell, fontsize=9, fontname="hebo" if r == 0 else "helv"
            )
    bottom = top + row_h * (len(rows) + 1)
    if ruled:
        for r in range(len(rows) + 2):
            page.draw_line((xs[0], top + r * row_h), (xs[-1], top + r * row_h))
        for x in xs:
            page.draw_line((x, top), (x, bottom))
    if after:
        page.insert_textbox(pymupdf.Rect(50, bottom + 20, 545, bottom + 100), after, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out, (xs[0], top, xs[-1], bottom)


FIGURE_LABELS = ("Encoder", "Attention", "Decoder")


def make_figure_pdf(
    caption: str | None = "Figure 1: The encoder-decoder model.",
    *,
    before: str | None = None,
    after: str | None = None,
    full_page: bool = False,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """One page: an optional paragraph, a drawn diagram (three labelled boxes joined by lines), an optional
    caption line under it and an optional paragraph after. With `full_page` the diagram fills the page.
    Returns the PDF and the diagram's box."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60.0
    if before:
        page.insert_textbox(pymupdf.Rect(50, y, 545, y + 60), before, fontsize=10)
        y += 80
    box = (40.0, 40.0, 555.0, 800.0) if full_page else (120.0, y, 470.0, y + 120)
    w = (box[2] - box[0] - 40) / 3
    for i, label in enumerate(FIGURE_LABELS):
        x0 = box[0] + 10 + i * (w + 10)
        top = (box[1] + box[3]) / 2 - 25
        page.draw_rect(pymupdf.Rect(x0, top, x0 + w, top + 50))
        page.insert_text((x0 + 8, top + 30), label, fontsize=11)
        if i:
            page.draw_line((x0 - 10, top + 25), (x0, top + 25))
    if full_page:
        page.insert_text((60, 70), "A whole-slide diagram of the model and its parts", fontsize=12)
    y = box[3] + 18
    if caption:
        page.insert_text((120, y), caption, fontsize=10)
        y += 20
    if after:
        page.insert_textbox(pymupdf.Rect(50, y, 545, y + 80), after, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out, box


def rasterized(
    draw: Callable[[pymupdf.Page], None], clip: tuple | None = None, dpi: int = 150
) -> pymupdf.Pixmap:
    """Whatever `draw` puts on an A4 page (or the `clip` of it), as a picture: its text is in the pixels only."""
    src = pymupdf.open()
    draw(src.new_page())
    pix = src[0].get_pixmap(dpi=dpi, clip=clip)
    src.close()
    return pix


SCAN_HEADER = "Week 6 handout: Collaborative Software Development, read before class"
SCAN_FOOTER = "Bring your team's backlog to the session. Questions go to the course forum."
SCAN_BOX = (40.0, 120.0, 555.0, 700.0)  # where make_scanned_pdf(header=...) puts the scanned picture


def make_scanned_pdf(
    title: str, bullets: list[str], *, hidden: bool = False, header: bool = False, tiles: int = 1
) -> bytes:
    """One page holding a picture of a slide (a title and bullets): a scan. With `hidden`, the scanner's
    invisible OCR layer (render mode 3) is laid over it; with `header`, the picture sits in SCAN_BOX between a
    typed header and footer (a picture of text on a page with a text layer); `tiles` cuts it into strips."""

    def slide(page: pymupdf.Page) -> None:
        page.insert_text((60, 120), title, fontsize=28)
        for i, b in enumerate(bullets):
            page.insert_text((70, 200 + i * 40), f"• {b}", fontsize=16)

    doc = pymupdf.open()
    page = doc.new_page()
    box = pymupdf.Rect(SCAN_BOX) if header else pymupdf.Rect(0, 0, 595, 842)
    for i in range(tiles):  # strip i: the same share of the slide and of the box
        clip = (0, 842 * i / tiles, 595, 842 * (i + 1) / tiles)
        strip = pymupdf.Rect(
            box.x0, box.y0 + box.height * i / tiles, box.x1, box.y0 + box.height * (i + 1) / tiles
        )
        page.insert_image(strip, pixmap=rasterized(slide, clip), keep_proportion=False)
    if hidden:  # over the picture, where the scanner found each line
        sx, sy = box.width / 595, box.height / 842
        page.insert_text((box.x0 + 60 * sx, box.y0 + 120 * sy), title, fontsize=28 * sy, render_mode=3)
        for i, b in enumerate(bullets):
            page.insert_text(
                (box.x0 + 70 * sx, box.y0 + (200 + i * 40) * sy), f"• {b}", fontsize=16 * sy, render_mode=3
            )
    if header:
        page.insert_text((40, 70), SCAN_HEADER, fontsize=12)
        page.insert_text((40, 760), SCAN_FOOTER, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out


def make_mixed_pdf(kinds: str) -> bytes:
    """One page per letter: 't' has a text paragraph, 'i' only an image (a scan without OCR)."""
    doc = pymupdf.open()
    for i, kind in enumerate(kinds):
        page = doc.new_page()
        if kind == "t":
            page.insert_textbox(
                pymupdf.Rect(50, 50, 545, 300),
                f"Page {i + 1}. Chlorophyll absorbs red and blue light and reflects green. " * 3,
            )
        else:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
            pix.clear_with(200)
            page.insert_image(pymupdf.Rect(50, 50, 545, 700), pixmap=pix)
    out = doc.tobytes()
    doc.close()
    return out


class FakeReranker:
    """httpx2 transport handler standing in for Voyage's rerank API. With no `handler` it is down
    (connection refused), so no test ever reaches the real, paid API. Records the request bodies."""

    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response] | None = None) -> None:
        self.handler = handler
        self.requests: list[dict] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        if self.handler is None:
            raise httpx2.ConnectError("reranker down (test)", request=request)
        return self.handler(request)


class FakeOpenAI:
    """httpx2 transport handler: replies with queued responses and records the request bodies."""

    def __init__(self) -> None:
        self.replies: list[httpx2.Response | Callable[[httpx2.Request], httpx2.Response]] = []
        self.requests: list[dict] = []
        self.timeouts: list[float] = []  # read timeout of each request
        self.sleeps: list[float] = []  # retry backoff delays (never really slept)

    def reply(self, status: int = 200, *, times: int = 1, **kwargs: Any) -> None:
        self.replies += [httpx2.Response(status, **kwargs) for _ in range(times)]

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content or b"{}"))
        self.timeouts.append(request.extensions["timeout"]["read"])
        reply = self.replies.pop(0)
        return reply(request) if callable(reply) else reply


def embedding_response(
    vectors: list[list[float]], *, prompt_tokens: int = 8, order: list[int] | None = None
) -> dict:
    """An embeddings response with base64 vectors, as OpenAI returns for the SDK's default request.
    `order` lists the indexes in the order they appear in `data`."""

    def b64(v: list[float]) -> str:
        return base64.b64encode(array.array("f", v).tobytes()).decode()

    return {
        "object": "list",
        "model": "text-embedding-3-small",
        "data": [
            {"object": "embedding", "index": i, "embedding": b64(vectors[i])}
            for i in (order if order is not None else range(len(vectors)))
        ],
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


USAGE = {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127}
CACHED_USAGE = {**USAGE, "prompt_tokens_details": {"cached_tokens": 100}}


def chat_response(
    content: str | None, *, finish_reason: str = "stop", refusal: str | None = None, usage: dict = USAGE
) -> dict:
    """A non-streamed chat completion."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": usage,
    }


def chat_stream(
    deltas: list[str],
    *,
    usage: dict | None = USAGE,
    fail_after: int | None = None,
    failure: type[httpx2.TransportError] = httpx2.ReadError,
) -> httpx2.Response:
    """A streamed chat completion: one SSE chunk per delta, a final finish_reason chunk, the `choices: []`
    usage chunk and [DONE]. `fail_after=n` breaks the connection after n deltas, with `failure`."""

    def event(choices: list[dict], **extra: Any) -> bytes:
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-test",
            "choices": choices,
            **extra,
        }
        return f"data: {json.dumps(chunk)}\n\n".encode()

    async def body() -> AsyncIterator[bytes]:
        for i, text in enumerate(deltas):
            if i == fail_after:
                raise failure("connection reset (test)")
            yield event([{"index": 0, "delta": {"content": text}, "finish_reason": None}])
        yield event([{"index": 0, "delta": {}, "finish_reason": "stop"}])
        if usage is not None:
            yield event([], usage=usage)
        yield b"data: [DONE]\n\n"

    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body())


@pytest.fixture
def reranker_fake(monkeypatch: pytest.MonkeyPatch) -> FakeReranker:
    """rerank's client sends to a FakeReranker (down until a test gives it a handler)."""
    fake = FakeReranker()
    client = httpx2.AsyncClient(base_url="https://voyage.test/v1", transport=httpx2.MockTransport(fake))
    monkeypatch.setattr(rerank, "_client", client)
    return fake


@pytest.fixture
def openai_fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    """Points openai_client at a mock transport; retry sleeps are recorded, not slept."""
    fake = FakeOpenAI()

    async def fake_sleep(delay: float) -> None:
        fake.sleeps.append(delay)

    client = AsyncOpenAI(
        api_key="sk-test", max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(fake))
    )
    monkeypatch.setattr(openai_client, "_client", client)
    monkeypatch.setattr(openai_client, "_sleep", fake_sleep)
    return fake


def fake_embeddings(request: httpx2.Request) -> httpx2.Response:
    """Replies to any embeddings request with one 1536-float vector per input."""
    inputs = json.loads(request.content)["input"]
    return httpx2.Response(
        200, json=embedding_response([[0.01 * (i % 7 + 1)] * 1536 for i in range(len(inputs))])
    )


def page_key0(path: Path) -> str:
    """The vision cache key of a PDF's first page (The cache is keyed by page content)."""
    from ingest.pages import page_key

    with pymupdf.open(path) as doc:
        return page_key(doc[0])


_EMPTY: dict[str, Any] = {
    "openai_api_key": None,
    "embedding_model": "text-embedding-3-small",
    "chat_model": "gpt-test",
    "chat_reasoning_effort": "none",
    "voyage_api_key": None,
    "voyage_base_url": "https://voyage.test/v1",
    "reranker_model": "rerank-test",
    "table_vision": True,
    "table_vision_model": "vision-model",
    "table_vision_max_pages": 500,
    "figures": True,
    "figure_model": "figure-model",
    "ocr": True,
    "cache_dir": Path("/nonexistent"),
}


# ---- POST /v1/ingest helpers; the vision pass is off unless a test turns it on (`with_vision`)

SENTENCE = "Chloroplasts capture light energy and convert it into chemical energy for the plant. "


def textbook(pages: int = 3, per_page: int = 7) -> bytes:
    return make_pdf(
        paragraphs=[
            [f"Page {p + 1}, paragraph {i + 1}. " + SENTENCE * 4 for i in range(per_page)]
            for p in range(pages)
        ]
    )


def post(
    client: TestClient, data: bytes, *, title: str = "Biology", content_type: str = "application/pdf"
) -> Response:
    return client.post(
        "/v1/ingest",
        params={"title": title, **BILLED_PARAMS},
        content=data,
        headers={"content-type": content_type},
    )


def ingested(client: TestClient, data: bytes, **kw: str) -> dict:
    resp = post(client, data, **kw)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def vision_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "vision_config", lambda settings: None)


def with_vision(monkeypatch: pytest.MonkeyPatch, cfg: VisionConfig) -> None:
    monkeypatch.setattr(main, "vision_config", lambda settings: cfg)


@pytest.fixture
def embeddings(openai_fake: FakeOpenAI) -> FakeOpenAI:
    openai_fake.replies += [fake_embeddings] * 5
    return openai_fake
