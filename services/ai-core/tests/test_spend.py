"""Per-user spend: every paid call is charged to the user it was made for, and a user over today's allowance is
refused before any provider call (ADR 0003)."""

import time

import httpx2
import pytest
from conftest import (
    BILLED,
    FakeOpenAI,
    FakeReranker,
    MemoryLedger,
    chat_response,
    chat_stream,
    configured,
    embedding_response,
)
from fastapi.testclient import TestClient

import billing
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
    record.of("describe").model, record.of("describe").calls = "gpt-5.4-mini", 1
    record.of("describe").prompt_tokens = 500
    record.of("cached").model = "gpt-6-luna"  # only cache hits: nothing to pay
    record.embed_tokens = 3000
    charges = billing.ingest_charges(record)
    assert [(c.model, c.tokens_in, c.tokens_out) for c in charges] == [
        ("gpt-5.4-mini", 1500, 200),
        ("text-embedding-3-small", 3000, 0),
    ]


def test_price_overrides_are_read_from_json() -> None:
    assert settings._prices('{"gpt-x": [1, 0.5, "2"]}') == {"gpt-x": (1.0, 0.5, 2.0)}
    assert settings._prices("  ") == {}
