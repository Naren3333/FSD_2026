"""The ingest cost record (RAG_PLAN Iteration 15, Step 3): calls, hits, failures, tokens and retries by kind."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingest import costs, vision
from conftest import BILLED_PARAMS, FakeOpenAI, ingested, make_pdf, page_key0, textbook

MODEL = "vision-model"
EMPTY = '{"tables": [], "figures": []}'


def reply(content: str = EMPTY, prompt: int = 1200, cached: int = 0, completion: int = 40) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


def pdf(tmp_path: Path, name: str = "doc.pdf", text: str = "Some text on the page.") -> Path:
    path = tmp_path / name
    path.write_bytes(make_pdf(paragraphs=[[text]]))
    return path


def see(path: Path, cache: Path) -> costs.IngestCost:
    async def run() -> costs.IngestCost:
        with costs.recording("doc") as record:
            await vision.see_pages(path, model=MODEL, cache_dir=cache, max_pages=500)
        return record

    return asyncio.run(run())


def test_a_model_call_and_a_cache_hit_are_counted_by_kind(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    cache = tmp_path / "cache"
    openai_fake.reply(json=reply(prompt=1200, cached=200, completion=40))
    first = see(pdf(tmp_path), cache)
    page = first.kinds["page"]
    assert (page.model, page.calls, page.hits, page.failed, page.retries) == (MODEL, 1, 0, 0, 0)
    assert (page.prompt_tokens, page.cached_tokens, page.completion_tokens) == (1200, 200, 40)
    assert page.seconds >= 0

    again = see(pdf(tmp_path), cache).kinds["page"]  # answered by the cache: no request
    assert (again.model, again.calls, again.hits, again.prompt_tokens) == (MODEL, 0, 1, 0)
    assert len(openai_fake.requests) == 1


def test_a_failed_call_is_counted_with_its_retries(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    """The page then goes without vision and the ingest carries on; the record is where the loss shows."""
    openai_fake.reply(500, json={"error": {"message": "boom"}}, times=3)  # the first try and both retries
    page = see(pdf(tmp_path), tmp_path / "cache").kinds["page"]
    assert (page.calls, page.failed, page.retries, page.prompt_tokens) == (0, 1, 2, 0)
    assert not vision.cache_file(tmp_path / "cache", MODEL, page_key0(pdf(tmp_path))).exists()


def test_an_answer_that_is_not_json_counts_as_a_call_and_a_failure(
    tmp_path: Path, openai_fake: FakeOpenAI
) -> None:
    openai_fake.reply(json=reply("sorry, I can't"))
    page = see(pdf(tmp_path), tmp_path / "cache").kinds["page"]
    assert (page.calls, page.failed, page.prompt_tokens) == (1, 1, 1200)


def test_a_rate_limited_call_that_then_succeeds_counts_its_retry(
    tmp_path: Path, openai_fake: FakeOpenAI
) -> None:
    openai_fake.reply(
        429,
        json={"error": {"message": "slow down", "code": "rate_limit_exceeded"}},
        headers={"retry-after-ms": "10"},
    )
    openai_fake.reply(json=reply())
    page = see(pdf(tmp_path), tmp_path / "cache").kinds["page"]
    assert (page.calls, page.failed, page.retries) == (1, 0, 1)


def test_without_a_record_nothing_is_recorded(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=reply())
    assert asyncio.run(
        vision.see_pages(pdf(tmp_path), model=MODEL, cache_dir=tmp_path / "cache", max_pages=500)
    ) == [vision.PageVision()]
    assert costs.current() is None


def test_write_logs_and_appends_a_json_line(tmp_path: Path) -> None:
    record = costs.IngestCost("doc-1", title="Biology", outcome="indexed", pages=3, chunks=5)
    record.of("describe").calls = 2
    record.stages["total"] = 1.23456
    log = tmp_path / "logs" / "costs.jsonl"
    costs.write(record, log)
    costs.write(record, log)
    costs.write(record, None)  # logged only
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["document_id"] == "doc-1" and row["outcome"] == "indexed" and row["stages"] == {"total": 1.23}
    assert row["kinds"]["describe"]["calls"] == 2 and row["kinds"]["describe"]["hits"] == 0


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The cost records /v1/ingest writes (logged; ai-core keeps no cost file)."""
    out: list[dict] = []
    monkeypatch.setattr(costs, "write", lambda record, path: out.append(record.to_json()))
    return out


def test_every_ingest_logs_its_record(
    api: TestClient,
    embeddings: FakeOpenAI,  # noqa: F811
    written: list[dict],
) -> None:
    doc = ingested(api, textbook(), title="Cells")
    [row] = written
    assert row["outcome"] == "indexed" and row["failure_reason"] is None
    assert (
        row["title"] == "Cells"
        and row["pages"] == 3
        and row["chunks"] == row["embed_inputs"] == len(doc["chunks"])
    )
    assert {"vision", "parse", "embed", "total"} <= set(row["stages"])
    assert row["kinds"] == {}  # vision is off in these tests


def test_a_failed_ingest_records_its_reason(
    api: TestClient,
    embeddings: FakeOpenAI,  # noqa: F811
    written: list[dict],
) -> None:
    doc = ingested(api, make_pdf(password="secret"))
    assert doc["outcome"] == "failed"
    [row] = written
    assert (row["outcome"], row["failure_reason"]) == ("failed", "ENCRYPTED")


def test_a_provider_error_is_recorded_as_an_error(
    api: TestClient, openai_fake: FakeOpenAI, written: list[dict]
) -> None:
    openai_fake.reply(400, json={"error": {"message": "bad request"}})
    assert (
        api.post(
            "/v1/ingest",
            params={"title": "T", **BILLED_PARAMS},
            content=textbook(),
            headers={"content-type": "application/pdf"},
        ).status_code
        == 502
    )
    [row] = written
    assert (row["outcome"], row["failure_reason"]) == ("error", None)
