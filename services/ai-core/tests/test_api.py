"""POST /v1/inference and /v1/rerank against mock providers, and who may call them."""

import json

import httpx2
import pytest
from conftest import SERVICE_CLAIMS, FakeOpenAI, FakeReranker, chat_response, chat_stream, embedding_response
from fastapi.testclient import TestClient
from learning_transport import actor

from main import app

EMBED = {"operation": "embed", "input": "Authorized material"}
GENERATE = {"operation": "generate", "input": "Say hello"}
RERANK = {
    "query": "photosynthesis stages",
    "documents": ["light reactions", "the Calvin cycle", "cell walls"],
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


def test_a_stream_that_breaks_ends_with_an_error_event(api: TestClient, openai_fake: FakeOpenAI) -> None:
    openai_fake.replies.append(chat_stream(["one", "two"], fail_after=1))
    resp = api.post("/v1/inference", json={**GENERATE, "stream": True})
    events = [block.split("\n")[0] for block in resp.text.strip().split("\n\n")]
    assert events == ["event: delta", "event: error"] and "AI_PROVIDER_FAILED" in resp.text


def test_a_stream_that_cannot_start_is_a_plain_error(api: TestClient, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(400, json={"error": {"message": "bad"}})
    assert error(api.post("/v1/inference", json={**GENERATE, "stream": True})) == (502, "AI_PROVIDER_FAILED")


def test_provider_failure_and_missing_key(api: TestClient, openai_fake: FakeOpenAI, monkeypatch) -> None:
    openai_fake.reply(500, json={"error": {"message": "boom"}}, times=3)
    assert error(api.post("/v1/inference", json=EMBED)) == (502, "AI_PROVIDER_FAILED")

    import openai_client
    from conftest import configured

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
    assert reranker_fake.requests == [{**RERANK, "model": "rerank-test", "truncation": True}]


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
    api: TestClient, reranker_fake: FakeReranker, handler
) -> None:
    reranker_fake.handler = handler
    assert error(api.post("/v1/rerank", json=RERANK)) == (502, "AI_PROVIDER_FAILED")


def test_rerank_without_a_key(api: TestClient, monkeypatch) -> None:
    import rerank
    from conftest import configured

    monkeypatch.setattr(rerank, "_client", None)
    monkeypatch.setattr(rerank, "get_settings", lambda: configured(voyage_api_key=None))
    assert error(api.post("/v1/rerank", json=RERANK)) == (503, "AI_PROVIDER_NOT_CONFIGURED")


@pytest.mark.parametrize(
    "body",
    [{**RERANK, "documents": []}, {**RERANK, "documents": [""]}, {**RERANK, "query": ""}, {"query": "q"}],
)
def test_rerank_requests_outside_the_contract(api: TestClient, body: dict) -> None:
    assert error(api.post("/v1/rerank", json=body)) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize(("path", "body"), [("/v1/inference", EMBED), ("/v1/rerank", RERANK)])
def test_no_anonymous_or_people_access(path: str, body: dict) -> None:
    client = TestClient(app)
    assert client.post(path, json=body).status_code == 401
    for role in ("student", "teacher"):
        app.dependency_overrides[actor] = lambda: {**SERVICE_CLAIMS, "realm_access": {"roles": [role]}}
        try:
            assert client.post(path, json=body).status_code == 403
        finally:
            app.dependency_overrides.clear()


def test_ready_reports_only_whether_keys_are_set(api: TestClient) -> None:
    assert api.get("/health/ready").json() == {
        "status": "ready",
        "openai": "configured",
        "reranker": "configured",
    }
