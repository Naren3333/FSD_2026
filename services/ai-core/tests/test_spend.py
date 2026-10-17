"""Per-user spend: every paid call is charged to the user it was made for, and a user over today's allowance is
refused before any provider call (ADR 0003)."""

import asyncio
import inspect
import time
from collections.abc import AsyncGenerator

import httpx2
import pytest
from conftest import (
    BILLED,
    CACHED_USAGE,
    FakeOpenAI,
    FakeReranker,
    MemoryLedger,
    chat_response,
    chat_stream,
    configured,
    embedding_response,
)
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from starlette.types import Message

import billing
import openai_client
import settings
import spend
from ingest import costs

EMBED = {"operation": "embed", "input": "Authorized material", "billed_to": BILLED}
GENERATE = {"operation": "generate", "input": "Say hello", "billed_to": BILLED}
RERANK = {
    "query": "photosynthesis",
    "passages": [
        {"title": "Plants", "content": "light reactions"},
        {"title": "Cells", "content": "cell walls"},
    ],
    "billed_to": BILLED,
}


def settle(ledger: MemoryLedger, n: int) -> None:
    """Charges are written by a background task: wait until `n` have landed."""
    deadline = time.monotonic() + 2
    while len(ledger.charges) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(ledger.charges) == n, ledger.charges


def test_prices() -> None:
    assert spend.cost("gpt-6-luna", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert spend.cost("gpt-5.4-mini", 1_000_000, 0, cached_in=1_000_000) == pytest.approx(0.075)
    assert spend.cost("text-embedding-3-small", 1_000_000) == pytest.approx(0.02)
    # An unknown model is never free: it is charged the most expensive known rates.
    assert spend.cost("gpt-next", 1_000_000, 1_000_000) == pytest.approx(0.75 + 4.50)


def test_price_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    overridden = configured(model_prices={"gpt-6-luna": (1, 0.5, 2)})
    monkeypatch.setattr("spend.get_settings", lambda: overridden)
    assert spend.cost("gpt-6-luna", 1_000_000, 1_000_000) == pytest.approx(3.0)


def test_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (spend.limit("student"), spend.limit("teacher")) == (5.0, 10.0)
    lower = configured(spend_limit_student_usd=2.5)
    monkeypatch.setattr("spend.get_settings", lambda: lower)
    assert spend.limit("student") == 2.5


def test_embed_is_charged(api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger) -> None:
    openai_fake.reply(json=embedding_response([[0.5, 0.25]], prompt_tokens=1000))
    assert api.post("/v1/inference", json=EMBED).status_code == 200
    settle(ledger, 1)
    billed, charge = ledger.charges[0]
    assert billed.user_id == "student-1" and charge.operation == "embed" and charge.tokens_in == 1000
    assert charge.cost_usd == pytest.approx(spend.cost(charge.model, 1000))


def test_generate_is_charged(api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger) -> None:
    openai_fake.reply(json=chat_response("Hello!"))
    assert api.post("/v1/inference", json=GENERATE).status_code == 200
    settle(ledger, 1)
    assert ledger.charges[0][1].operation == "generate" and not ledger.charges[0][1].estimated


def test_cached_prompt_tokens_are_charged_at_the_cached_rate(
    api: TestClient,
    openai_fake: FakeOpenAI,
    ledger: MemoryLedger,
    test_settings: settings.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priced = configured(model_prices={test_settings.chat_model: (1.0, 0.1, 2.0)})
    monkeypatch.setattr("spend.get_settings", lambda: priced)
    openai_fake.reply(json=chat_response("Hello!", usage=CACHED_USAGE))
    openai_fake.replies.append(chat_stream(["Hel", "lo"], usage=CACHED_USAGE))
    assert api.post("/v1/inference", json=GENERATE).status_code == 200
    assert api.post("/v1/inference", json={**GENERATE, "stream": True}).status_code == 200
    settle(ledger, 2)
    expected = spend.cost(test_settings.chat_model, 120, 7, cached_in=100)
    assert expected < spend.cost(test_settings.chat_model, 120, 7)
    for _, charge in ledger.charges:
        assert (charge.tokens_in, charge.tokens_out) == (120, 7)
        assert charge.cost_usd == pytest.approx(expected)


def test_an_estimated_reply_has_no_cached_tokens() -> None:
    usage = openai_client.Usage(None, 7, cached_in=100)
    charge = billing.reply_charge("stream", "gpt-6-luna", "abcd" * 10, "reply", usage)
    assert charge.estimated and charge.tokens_in == 10
    assert charge.cost_usd == pytest.approx(spend.cost("gpt-6-luna", 10, 7))


def test_a_stream_is_charged_once(api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger) -> None:
    openai_fake.replies.append(chat_stream(["Hel", "lo"]))
    assert api.post("/v1/inference", json={**GENERATE, "stream": True}).status_code == 200
    settle(ledger, 1)
    charge = ledger.charges[0][1]
    assert charge.operation == "stream" and charge.tokens_out > 0


def test_rerank_is_charged(api: TestClient, reranker_fake: FakeReranker, ledger: MemoryLedger) -> None:
    reranker_fake.handler = lambda r: httpx2.Response(
        200,
        json={
            "data": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}],
            "usage": {"total_tokens": 40},
        },
    )
    assert api.post("/v1/rerank", json=RERANK).status_code == 200
    settle(ledger, 1)
    assert ledger.charges[0][1].tokens_in == 40


@pytest.mark.parametrize(("path", "body"), [("/v1/inference", EMBED), ("/v1/rerank", RERANK)])
def test_over_the_limit_nothing_is_called(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger, path: str, body: dict
) -> None:
    ledger.already["student-1"] = 5.0
    resp = api.post(path, json=body)
    assert resp.status_code == 429
    error = resp.json()["error"]
    assert error["code"] == "AI_SPEND_LIMIT" and error["limit_usd"] == 5.0 and error["resets_at"]
    assert openai_fake.requests == [] and ledger.charges == []


def test_a_teacher_has_the_larger_allowance(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger
) -> None:
    ledger.already["teacher-1"] = 5.0
    openai_fake.reply(json=embedding_response([[0.5]], prompt_tokens=3))
    teacher = {**EMBED, "billed_to": {"user_id": "teacher-1", "org_id": "test-org", "role": "teacher"}}
    assert api.post("/v1/inference", json=teacher).status_code == 200


def test_no_ledger_no_call(api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger) -> None:
    ledger.broken = True
    resp = api.post("/v1/inference", json=EMBED)
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "USAGE_LEDGER_UNAVAILABLE")
    assert openai_fake.requests == []
    assert api.get("/health/ready").status_code == 503


def test_unbilled_calls_are_refused(api: TestClient) -> None:
    body = {k: v for k, v in EMBED.items() if k != "billed_to"}
    assert api.post("/v1/inference", json=body).status_code == 422


def test_usage(api: TestClient, ledger: MemoryLedger) -> None:
    ledger.already["student-1"] = 1.25
    resp = api.get("/v1/usage", params={"user_id": "student-1", "org_id": "test-org", "role": "student"})
    assert resp.status_code == 200
    assert resp.json()["spent_usd"] == 1.25 and resp.json()["limit_usd"] == 5.0


def test_ingest_charges_per_model() -> None:
    record = costs.IngestCost("doc")
    record.of("page").model, record.of("page").calls = "gpt-5.4-mini", 2
    record.of("page").prompt_tokens, record.of("page").completion_tokens = 1000, 200
    record.of("page").cached_tokens = 400
    record.of("describe").model, record.of("describe").calls = "gpt-5.4-mini", 1
    record.of("describe").prompt_tokens = 500
    record.of("cached").model = "gpt-6-luna"  # only cache hits: nothing to pay
    record.embed_tokens = 3000
    charges = billing.ingest_charges(record)
    assert [(c.model, c.tokens_in, c.tokens_out) for c in charges] == [
        ("gpt-5.4-mini", 1500, 200),
        ("text-embedding-3-small", 3000, 0),
    ]
    assert charges[0].cost_usd == pytest.approx(spend.cost("gpt-5.4-mini", 1500, 200, cached_in=400))


def test_price_overrides_are_read_from_json() -> None:
    assert settings._prices('{"gpt-x": [1, 0.5, "2"]}') == {"gpt-x": (1.0, 0.5, 2.0)}
    assert settings._prices("  ") == {}


SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "reply",
    [chat_response(None, refusal="I can't help with that"), chat_response('{"que', finish_reason="length")],
    ids=["refused", "cut-off"],
)
def test_a_rejected_structured_reply_is_still_charged(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger, reply: dict
) -> None:
    """OpenAI bills it like any other reply; the error code stays AI_OUTPUT_REJECTED (Grading doesn't retry it)."""
    openai_fake.reply(json=reply)
    resp = api.post("/v1/inference", json={**GENERATE, "response_schema": SCHEMA})
    assert (resp.status_code, resp.json()["error"]["code"]) == (502, "AI_OUTPUT_REJECTED")
    settle(ledger, 1)
    charge = ledger.charges[0][1]
    assert (charge.operation, charge.tokens_in, charge.tokens_out, charge.estimated) == (
        "generate",
        120,
        7,
        False,
    )


def test_an_oversized_response_schema_is_refused(api: TestClient, openai_fake: FakeOpenAI) -> None:
    schema = {**SCHEMA, "description": "x" * 20000}
    resp = api.post("/v1/inference", json={**GENERATE, "response_schema": schema})
    assert resp.status_code == 422 and openai_fake.requests == []


def test_rerank_without_usage_is_charged_an_estimate(
    api: TestClient, reranker_fake: FakeReranker, ledger: MemoryLedger
) -> None:
    reranker_fake.handler = lambda r: httpx2.Response(
        200,
        json={
            "data": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}],
            "usage": None,
        },
    )
    resp = api.post("/v1/rerank", json=RERANK)
    assert resp.status_code == 200 and resp.json()["usage_tokens"] is None
    settle(ledger, 1)
    assert ledger.charges[0][1].estimated and ledger.charges[0][1].tokens_in > 0


def test_a_stream_the_caller_leaves_before_it_starts_is_charged_and_closed(
    openai_fake: FakeOpenAI, ledger: MemoryLedger
) -> None:
    """Starlette never starts the events generator when the caller is gone first: ClosingStream still bills the
    reply and closes the provider's stream."""
    import main

    openai_fake.replies.append(chat_stream(["Hel", "lo"]))
    billed = spend.Billed.model_validate(BILLED)

    async def run() -> AsyncGenerator[str | openai_client.Usage, None]:
        bills = billing.Billing(ledger)
        stream = openai_client.stream_chat([{"role": "user", "content": "Say hello"}], 20)
        first = await anext(stream)
        bill = billing.StreamBill(bills, billed, "corr", "gpt-test", "Say hello")

        async def finish() -> None:
            bill.close()
            await stream.aclose()

        response = main.ClosingStream(main.events(first, stream, bill), finish)

        async def receive() -> Message:
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            raise OSError("the caller is gone")

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        await bills.drain()
        return stream

    stream = asyncio.run(run())
    assert inspect.getasyncgenstate(stream) == inspect.AGEN_CLOSED
    [(_, charge)] = ledger.charges
    assert charge.operation == "stream" and charge.estimated


def test_ingest_rechecks_the_allowance_once_it_has_a_slot(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploads queued behind others passed the first check long ago; the one that finds the allowance used up
    when its turn comes makes no model call."""
    from conftest import post, textbook

    checks: list[str] = []
    today = ledger.today

    async def spent_meanwhile(billed: spend.Billed) -> spend.Usage:
        checks.append(billed.user_id)
        if len(checks) > 1:
            ledger.already[billed.user_id] = 10.0
        return await today(billed)

    monkeypatch.setattr(ledger, "today", spent_meanwhile)
    records: list[costs.IngestCost] = []
    monkeypatch.setattr(costs, "write", lambda record, path: records.append(record))
    resp = post(api, textbook())
    assert (resp.status_code, resp.json()["error"]["code"]) == (429, "AI_SPEND_LIMIT")
    assert checks == ["teacher-1", "teacher-1"] and openai_fake.requests == []
    assert records[0].outcome == "skipped" and "queued" in records[0].stages


def test_embedding_batches_answered_before_a_failure_are_charged(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import fake_embeddings, post, textbook

    monkeypatch.setattr(openai_client, "EMBED_BATCH", 1)
    openai_fake.replies.append(fake_embeddings)  # 8 prompt tokens
    openai_fake.reply(400, json={"error": {"message": "bad"}})
    resp = post(api, textbook())
    assert (resp.status_code, resp.json()["error"]["code"]) == (502, "AI_PROVIDER_FAILED")
    settle(ledger, 1)
    charge = ledger.charges[0][1]
    assert (charge.operation, charge.model, charge.tokens_in) == ("ingest", "text-embedding-3-small", 8)
