"""post /v1/ingest: a pdf in, chunks with vectors out."""

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    FakeOpenAI,
    configured,
    ingested,
    make_mixed_pdf,
    make_pdf,
    make_structured_pdf,
    make_table_pdf,
    post,
    textbook,
    with_vision,
)
from fastapi.testclient import TestClient
from httpx import Response
from learning_transport.testing import as_caller, token_claims

import main
from ingest import VisionConfig, vision
from ingest.chunker import MAX_TOKENS
from main import app

QUOTA = {
    "error": {
        "message": "You exceeded your current quota",
        "type": "insufficient_quota",
        "code": "insufficient_quota",
    }
}
CHUNK_FIELDS = {
    "ordinal", "content", "embed_text", "section_path", "page_start", "page_end", "token_count", "kind", "source",
    "image", "label", "cites", "embedding",
}  # fmt: skip


def test_text_pdf_is_indexed_with_chunks(api: TestClient, embeddings: FakeOpenAI) -> None:
    doc = ingested(api, textbook())
    assert doc["outcome"] == "indexed" and doc["failure_reason"] is None
    assert doc["page_count"] == 3 and doc["ocr_page_count"] == 0
    assert doc["embedding_model"] == "text-embedding-3-small" and doc["figures"] == []
    chunks = doc["chunks"]
    assert len(chunks) >= 2 and set(chunks[0]) == CHUNK_FIELDS
    assert [c["ordinal"] for c in chunks] == list(range(len(chunks)))
    assert all(c["section_path"] is None and 0 < c["token_count"] <= MAX_TOKENS for c in chunks)
    assert chunks[0]["page_start"] == 1 and chunks[-1]["page_end"] == 3
    assert chunks[0]["content"].startswith("Page 1, paragraph 1.")
    assert all(len(c["embedding"]) == 1536 for c in chunks)

    sent = embeddings.requests[0]["input"]
    assert sent == [c["embed_text"] for c in chunks]
    assert sent[0] == f"Biology\n\n{chunks[0]['content']}"


def test_the_same_pdf_twice_gives_the_same_result(api: TestClient, embeddings: FakeOpenAI) -> None:
    """stateless: a retried or duplicated job gets an identical answer, so the caller may store either."""
    data = textbook()
    assert ingested(api, data) == ingested(api, data)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (make_pdf(password="secret"), "ENCRYPTED"),
        (make_mixed_pdf("iit"), "NO_TEXT_LAYER"),
        (b"%PDF-1.7\n" + bytes(range(256)) * 16, "CORRUPT"),
    ],
    ids=["encrypted", "scanned", "corrupt"],
)
def test_unreadable_pdfs_fail_with_their_reason(
    api: TestClient, embeddings: FakeOpenAI, data: bytes, reason: str
) -> None:
    doc = ingested(api, data)
    assert (doc["outcome"], doc["failure_reason"]) == ("failed", reason)
    assert doc["failure_detail"] and doc["chunks"] == [] and doc["page_count"] is None
    assert embeddings.requests == []


@pytest.mark.parametrize(
    ("data", "reason"),
    [(make_pdf(password="secret"), "ENCRYPTED"), (b"%PDF-1.7\n" + bytes(range(256)) * 16, "CORRUPT")],
    ids=["encrypted", "corrupt"],
)
def test_unreadable_pdfs_fail_before_the_vision_pass(
    api: TestClient,
    embeddings: FakeOpenAI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    reason: str,
) -> None:
    async def see_pages(*args: object, **kwargs: object) -> None:
        raise AssertionError("the vision pass ran on an unreadable PDF")

    with_vision(monkeypatch, VisionConfig("vision-model", tmp_path / "vision", 500))
    monkeypatch.setattr(vision, "see_pages", see_pages)
    doc = ingested(api, data)
    assert (doc["outcome"], doc["failure_reason"]) == ("failed", reason)


def test_a_pdf_over_the_page_cap_fails_before_any_model_call(
    api: TestClient, embeddings: FakeOpenAI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def see_pages(*args: object, **kwargs: object) -> None:
        raise AssertionError("the vision pass ran on an oversized PDF")

    monkeypatch.setattr("ingest.get_settings", lambda: configured(ingest_max_pages=2))
    with_vision(monkeypatch, VisionConfig("vision-model", tmp_path / "vision", 500))
    monkeypatch.setattr(vision, "see_pages", see_pages)
    doc = ingested(api, make_pdf(pages=3))
    assert (doc["outcome"], doc["failure_reason"]) == ("failed", "TOO_LARGE")
    assert "3 pages" in doc["failure_detail"] and embeddings.requests == []


def test_a_pdf_over_the_chunk_cap_fails_before_its_embeddings(
    api: TestClient, embeddings: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ingest.get_settings", lambda: configured(ingest_max_chunks=1))
    doc = ingested(api, textbook())  # at least two chunks
    assert (doc["outcome"], doc["failure_reason"]) == ("failed", "TOO_LARGE")
    assert embeddings.requests == []


def test_provider_errors_are_errors_not_failed_documents(api: TestClient, openai_fake: FakeOpenAI) -> None:
    """a quota or outage is the caller's to retry: it never marks the document failed or returns partial output."""
    openai_fake.reply(429, json=QUOTA)
    resp = post(api, textbook())
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "AI_PROVIDER_FAILED"
    assert "chunks" not in resp.json()


def test_missing_key_is_503(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import openai_client

    monkeypatch.setattr(openai_client, "_client", None)
    monkeypatch.setattr(openai_client, "get_settings", lambda: configured(openai_api_key=None))
    resp = post(api, make_pdf(password="secret"))  # refused before reading, even a pdf that needs no model
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "AI_PROVIDER_NOT_CONFIGURED"


LEAF = "Leaves hold many chloroplasts, and stomata on their underside let carbon dioxide in. " * 3


def structured() -> bytes:
    """four pages with a running footer: chapter 1 (two subsections) and chapter 2."""
    return make_structured_pdf(
        [
            ("page", ""), ("h1", "Chapter 1 Plants"), ("h2", "Leaves"), *[("p", LEAF)] * 3,
            ("page", ""), ("h2", "Roots"), *[("p", "Roots take up water and minerals from the soil. " * 3)] * 3,
            ("page", ""), ("h1", "Chapter 2 Animals"),
            *[("p", "Animals get energy by eating other organisms. " * 3)] * 3,
            ("page", ""), *[("p", "Herbivores eat plants and carnivores eat other animals. " * 3)] * 3,
        ],
        footer="Year 9 Biology - page #",
    )  # fmt: skip


def test_structured_pdf_gets_section_paths_and_no_footer(api: TestClient, embeddings: FakeOpenAI) -> None:
    doc = ingested(api, structured())
    assert doc["outcome"] == "indexed" and doc["page_count"] == 4
    chunks = doc["chunks"]
    assert [c["section_path"] for c in chunks] == ["Chapter 1 Plants", "Chapter 2 Animals"]
    assert chunks[0]["content"].startswith("Chapter 1 Plants\n\nLeaves\n\n")
    assert (chunks[0]["page_start"], chunks[0]["page_end"], chunks[1]["page_start"]) == (1, 2, 3)
    assert not any("Year 9" in c["content"] for c in chunks)
    sent = embeddings.requests[0]["input"]
    assert sent[1] == f"Biology\nChapter 2 Animals\n\n{chunks[1]['content']}" == chunks[1]["embed_text"]


def test_a_table_is_a_table_chunk(api: TestClient, embeddings: FakeOpenAI) -> None:
    data, _ = make_table_pdf(
        ["Criterion", "Band 4", "Band 2"],
        [
            ["Analysis", "Explains causes clearly", "Lists causes"],
            ["Evidence", "Cites three sources", "Cites one source"],
        ],
        before="Each criterion is marked in bands. " * 4,
        after="Marks are scaled to 40%. " * 4,
    )
    chunks = ingested(api, data)["chunks"]  # vision is off: the ruled table comes from "lines"
    assert [(c["kind"], c["source"]) for c in chunks] == [("text", "pdf"), ("table", "pdf"), ("text", "pdf")]
    assert "| Analysis | Explains causes clearly | Lists causes |" in chunks[1]["content"]
    assert "Analysis" not in chunks[0]["content"] + chunks[2]["content"]
    assert (
        "Criterion: Evidence; Band 4: Cites three sources; Band 2: Cites one source"
        in chunks[1]["embed_text"]
    )


# ---- the http contract


@pytest.mark.parametrize(
    ("send", "status", "code"),
    [
        (lambda c: post(c, textbook(), content_type="text/plain"), 415, "UNSUPPORTED_MEDIA_TYPE"),
        (lambda c: post(c, b""), 422, "VALIDATION_ERROR"),
        (lambda c: post(c, textbook(), title=""), 422, "VALIDATION_ERROR"),
        (lambda c: c.post("/v1/ingest", content=textbook(), headers={"content-type": "application/pdf"}), 422,
         "VALIDATION_ERROR"),
    ],
    ids=["not-pdf", "empty", "blank-title", "no-title"],
)  # fmt: skip
def test_bad_requests(
    api: TestClient, send: Callable[[TestClient], Response], status: int, code: str
) -> None:
    resp = send(api)
    assert (resp.status_code, resp.json()["error"]["code"]) == (status, code)


def test_more_than_50_mib_is_refused(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "MAX_PDF_BYTES", 1000)
    resp = post(api, textbook())
    assert (resp.status_code, resp.json()["error"]["code"]) == (413, "PAYLOAD_TOO_LARGE")


def test_people_cannot_ingest() -> None:
    client = TestClient(app)
    assert post(client, textbook()).status_code == 401
    with as_caller(app, token_claims("teacher", sub="content-indexer")):
        assert post(client, textbook()).status_code == 403
