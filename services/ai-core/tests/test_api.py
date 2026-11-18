"""post /v1/inference and /v1/rerank against mock providers, and who may call them."""

import json
from collections.abc import Callable

import httpx2
import psycopg
import pytest
from conftest import (
    BILLED,
    FakeOpenAI,
    FakeReranker,
    MemoryLedger,
    chat_response,
    chat_stream,
    embedding_response,
)
from fastapi.testclient import TestClient
from learning_transport import MONITORING_READER
from learning_transport.testing import as_caller, token_claims

from main import app

EMBED = {"operation": "embed", "input": "Authorized material", "billed_to": BILLED}
GENERATE = {"operation": "generate", "input": "Say hello", "billed_to": BILLED}
RERANK = {
    "query": "photosynthesis stages",
    "passages": [
        {"title": "Photosynthesis", "section_path": "Plants > Light reactions", "content": "ATP and NADPH"},
        {"title": "Photosynthesis", "section_path": None, "content": "the Calvin cycle"},
        {"title": "Cells", "content": "cell walls"},
    ],
    "billed_to": BILLED,
}
SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def error(resp: object) -> tuple[int, str]:
    return resp.status_code, resp.json()["error"]["code"]  # type: ignore[attr-defined]


def test_embed(api: TestClient, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=embedding_response([[0.5, 0.25]], prompt_tokens=3))
    resp = api.post("/v1/inference", json=EMBED)
    assert resp.status_code == 200
    assert resp.json() == {
        "output": None,
        "vectors": [[0.5, 0.25]],
        "model_version": "text-embedding-3-small",
        "usage_tokens": 3,
        "finish_reason": None,
    }
    assert openai_fake.requests[0]["input"] == ["Authorized material"]


def test_monitoring_reader_cannot_run_paid_operations(api: TestClient, openai_fake: FakeOpenAI) -> None:
    assert api.get("/internal/spend/summary").status_code == 403
    assert api.get("/internal/http-metrics").status_code == 403
    with as_caller(app, token_claims(MONITORING_READER)):
        before = api.get("/internal/http-metrics")
        assert before.status_code == 200
        assert before.json()["bounds_ms"] == [10, 50, 100, 500, 1000, 5000, 30000, 60000, 120000, 300000]
        assert api.get("/health/live").status_code == 200
        assert api.get("/internal/spend/summary").status_code == 503  # the test double has no report database
        assert api.post("/v1/inference", json=EMBED).status_code == 403
        assert api.post("/v1/rerank", json=RERANK).status_code == 403
        after = api.get("/internal/http-metrics").json()
        assert after["instance"] == before.json()["instance"]
        assert sum(after["bucket_counts"]) - sum(before.json()["bucket_counts"]) == 2
    assert not openai_fake.requests


@pytest.mark.parametrize(
    ("report", "exception", "expected"),
    [
        ("summary", TimeoutError, 503),
        ("users", psycopg.OperationalError, 503),
        ("summary", ValueError, 500),
    ],
)
def test_spend_failures_are_logged_without_private_details(
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    report: str,
    exception: type[Exception],
    expected: int,
) -> None:
    async def fail(*_args: object) -> None:
        raise exception("private billed account filter")

    monkeypatch.setattr("main.reporting_ledger", lambda _request: None)
    monkeypatch.setattr(f"spend_report.{report}", fail)
    with as_caller(app, token_claims(MONITORING_READER)):
        response = api.get(f"/internal/spend/{report}")
    assert response.status_code == expected
    record = next(record for record in caplog.records if record.message == "spend_report_failed")
    assert record.__dict__["report"] == report
    assert record.__dict__["error_category"] == exception.__name__
    assert record.__dict__["correlation_id"] == response.headers["X-Correlation-ID"]
    assert "private billed account filter" not in caplog.text + response.text


def test_generate_from_input(api: TestClient, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=chat_response("Hello!"))
    resp = api.post("/v1/inference", json={**GENERATE, "max_tokens": 20})
    assert resp.json() == {
        "output": "Hello!",
        "vectors": None,
        "model_version": "gpt-test",
        "usage_tokens": 127,
        "finish_reason": "stop",
    }
    [req] = openai_fake.requests
    assert (
        req["messages"] == [{"role": "user", "content": "Say hello"}] and req["max_completion_tokens"] == 20
    )


def test_generate_from_messages_with_a_schema(api: TestClient, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=chat_response('{"query": "stages of photosynthesis"}'))
    messages = [{"role": "system", "content": "Rewrite."}, {"role": "user", "content": "and its stages?"}]
    body = {
        "operation": "generate",
        "messages": messages,
        "response_schema": SCHEMA,
        "schema_name": "rewrite.v1",
        "billed_to": BILLED,
    }
    resp = api.post("/v1/inference", json=body)
    assert resp.status_code == 200 and json.loads(resp.json()["output"]) == {
        "query": "stages of photosynthesis"
    }
    [req] = openai_fake.requests
    assert req["messages"] == messages
    assert req["response_format"]["json_schema"] == {"name": "rewrite_v1", "strict": True, "schema": SCHEMA}


def test_a_refused_structured_reply_is_an_error(api: TestClient, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=chat_response(None, refusal="no"))
    body = {**GENERATE, "response_schema": SCHEMA}
    assert error(api.post("/v1/inference", json=body)) == (502, "AI_OUTPUT_REJECTED")


def test_streamed_reply(api: TestClient, openai_fake: FakeOpenAI) -> None:
    openai_fake.replies.append(chat_stream(["Photo", "synthesis"]))
    resp = api.post("/v1/inference", json={**GENERATE, "stream": True})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == (
        'event: delta\ndata: {"text":"Photo"}\n\n'
        'event: delta\ndata: {"text":"synthesis"}\n\n'
        'event: done\ndata: {"model_version":"gpt-test","usage_tokens":127}\n\n'
    )


def test_a_stream_that_breaks_ends_with_an_error_event(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger
) -> None:
    openai_fake.replies.append(chat_stream(["one", "two"], fail_after=1))
    resp = api.post("/v1/inference", json={**GENERATE, "stream": True})
    events = [block.split("\n")[0] for block in resp.text.strip().split("\n\n")]
    assert events == ["event: delta", "event: error"] and "AI_PROVIDER_FAILED" in resp.text
    # the text relayed before the break is still paid for: charged once, by estimate (no usage chunk came).
    [(_, charge)] = ledger.charges
    assert charge.operation == "stream" and charge.estimated
    assert charge.tokens_out == 1 and charge.tokens_in > 0 and charge.cost_usd > 0  # "one": about one token


def test_a_stream_that_cannot_start_is_a_plain_error(api: TestClient, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(400, json={"error": {"message": "bad"}})
    assert error(api.post("/v1/inference", json={**GENERATE, "stream": True})) == (502, "AI_PROVIDER_FAILED")


def test_provider_failure_and_missing_key(
    api: TestClient, openai_fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    openai_fake.reply(500, json={"error": {"message": "boom"}}, times=3)
    assert error(api.post("/v1/inference", json=EMBED)) == (502, "AI_PROVIDER_FAILED")

    from conftest import configured

    import openai_client

    monkeypatch.setattr(openai_client, "_client", None)
    monkeypatch.setattr(openai_client, "get_settings", lambda: configured(openai_api_key=None))
    assert error(api.post("/v1/inference", json=EMBED)) == (503, "AI_PROVIDER_NOT_CONFIGURED")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {**EMBED, "unknown": True},
        {**EMBED, "model_profile": "future-default"},
        {"operation": "embed", "messages": [{"role": "user", "content": "x"}]},
        {**EMBED, "stream": True},
        {"operation": "generate"},
        {**GENERATE, "messages": [{"role": "user", "content": "x"}]},
        {**GENERATE, "stream": True, "response_schema": SCHEMA},
        {**GENERATE, "max_tokens": 0},
        {"operation": "generate", "messages": [{"role": "tool", "content": "x"}]},
    ],
)
def test_requests_outside_the_contract(api: TestClient, body: dict) -> None:
    assert error(api.post("/v1/inference", json=body)) == (422, "VALIDATION_ERROR")


def test_rerank(api: TestClient, reranker_fake: FakeReranker) -> None:
    def scores(request: httpx2.Request) -> httpx2.Response:
        data = [{"index": 2, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.9}]
        return httpx2.Response(200, json={"data": [*data, {"index": 1, "relevance_score": 0.5}],
                                          "usage": {"total_tokens": 42}})  # fmt: skip

    reranker_fake.handler = scores
    resp = api.post("/v1/rerank", json=RERANK)
    assert resp.json() == {"scores": [0.9, 0.5, 0.1], "model_version": "rerank-test", "usage_tokens": 42}
    assert reranker_fake.requests == [
        {
            "query": RERANK["query"],
            "documents": [
                "Photosynthesis\nLight reactions\n\nATP and NADPH",
                "Photosynthesis\n\nthe Calvin cycle",
                "Cells\n\ncell walls",
            ],
            "model": "rerank-test",
            "truncation": True,
        }
    ]


@pytest.mark.parametrize(
    "handler",
    [
        None,  # down
        lambda r: httpx2.Response(500, json={}),
        lambda r: httpx2.Response(
            200, json={"data": [{"index": 0, "relevance_score": 1.0}]}
        ),  # scores missing
        lambda r: httpx2.Response(200, json={"data": [{"index": 9, "relevance_score": 1.0}]}),
        lambda r: httpx2.Response(200, text="not json"),
    ],
    ids=["down", "500", "missing", "out-of-range", "not-json"],
)
def test_rerank_failures_are_errors_never_scores(
    api: TestClient, reranker_fake: FakeReranker, handler: Callable[[httpx2.Request], httpx2.Response] | None
) -> None:
    reranker_fake.handler = handler
    assert error(api.post("/v1/rerank", json=RERANK)) == (502, "AI_PROVIDER_FAILED")


def test_rerank_without_a_key(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import configured

    import rerank

    monkeypatch.setattr(rerank, "_client", None)
    monkeypatch.setattr(rerank, "get_settings", lambda: configured(voyage_api_key=None))
    assert error(api.post("/v1/rerank", json=RERANK)) == (503, "AI_PROVIDER_NOT_CONFIGURED")


@pytest.mark.parametrize(
    "body",
    [
        {**RERANK, "passages": []},
        {**RERANK, "passages": [{"title": "T", "content": ""}]},
        {**RERANK, "passages": [{"title": "", "content": "c"}]},
        {**RERANK, "documents": ["the old shape"]},
        {**RERANK, "query": ""},
        {"query": "q"},
    ],
)
def test_rerank_requests_outside_the_contract(api: TestClient, body: dict) -> None:
    assert error(api.post("/v1/rerank", json=body)) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize(("path", "body"), [("/v1/inference", EMBED), ("/v1/rerank", RERANK)])
def test_no_anonymous_or_people_access(path: str, body: dict) -> None:
    client = TestClient(app)
    assert client.post(path, json=body).status_code == 401
    for role in ("student", "teacher"):
        with as_caller(app, token_claims(role, sub="content-indexer")):
            assert client.post(path, json=body).status_code == 403


def test_ready_reports_only_whether_keys_are_set(api: TestClient) -> None:
    assert api.get("/health/ready").json() == {
        "status": "ready",
        "openai": "configured",
        "reranker": "configured",
    }
