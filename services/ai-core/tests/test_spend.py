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
from fastapi import HTTPException
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


def hang(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("timed out (test)", request=request)


PROMPT = [{"role": "user", "content": "Say hello"}]


@pytest.mark.parametrize("stream", [False, True], ids=["reply", "stream-before-first-token"])
def test_a_timed_out_reply_is_charged_its_prompt_and_whole_output(
    api: TestClient,
    openai_fake: FakeOpenAI,
    ledger: MemoryLedger,
    test_settings: settings.Settings,
    stream: bool,
) -> None:
    """OpenAI may still run, and bill, a reply nobody waited for: it is charged as if it used all of max_tokens."""
    openai_fake.replies.append(hang)
    resp = api.post("/v1/inference", json={**GENERATE, "max_tokens": 6000, "stream": stream})
    assert (resp.status_code, resp.json()["error"]["code"]) == (502, "AI_PROVIDER_FAILED")
    settle(ledger, 1)
    billed, charge = ledger.charges[0]
    assert billed.user_id == "student-1" and charge.estimated
    assert (charge.operation, charge.tokens_in, charge.tokens_out) == (
        "stream" if stream else "generate",
        billing.prompt_tokens(PROMPT),
        6000,
    )
    assert charge.cost_usd == pytest.approx(spend.cost(test_settings.chat_model, charge.tokens_in, 6000))


def test_a_stream_that_times_out_midway_is_charged_its_whole_output(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger
) -> None:
    openai_fake.replies.append(chat_stream(["one", "two"], fail_after=1, failure=httpx2.ReadTimeout))
    resp = api.post("/v1/inference", json={**GENERATE, "max_tokens": 500, "stream": True})
    assert "event: error" in resp.text
    settle(ledger, 1)
    charge = ledger.charges[0][1]
    assert charge.operation == "stream" and charge.estimated
    assert (charge.tokens_in, charge.tokens_out) == (billing.prompt_tokens(PROMPT), 500)


def test_a_timed_out_embedding_is_charged_its_input(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger
) -> None:
    openai_fake.replies.append(hang)
    assert api.post("/v1/inference", json=EMBED).status_code == 502
    settle(ledger, 1)
    charge = ledger.charges[0][1]
    assert (charge.operation, charge.tokens_out, charge.estimated) == ("embed", 0, True)
    assert charge.tokens_in == billing.prompt_tokens([{"content": EMBED["input"]}])


def test_prompt_tokens_count_with_the_tokenizer() -> None:
    # Counted by cl100k_base, plus each message's framing; special-token text is just text.
    one = billing.prompt_tokens([{"role": "user", "content": "hello world"}])
    assert one == 2 + billing.MESSAGE_OVERHEAD
    assert billing.prompt_tokens([{"role": "user", "content": "<|endoftext|>"}]) > billing.MESSAGE_OVERHEAD


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


@pytest.mark.parametrize(
    ("role", "spent", "status"),
    [("student", 4.99, 200), ("student", 5.0, 429), ("teacher", 9.99, 200), ("teacher", 10.0, 429)],
)
def test_each_role_is_refused_exactly_at_its_allowance(
    api: TestClient, openai_fake: FakeOpenAI, ledger: MemoryLedger, role: str, spent: float, status: int
) -> None:
    ledger.already["user-1"] = spent
    openai_fake.reply(json=embedding_response([[0.5]], prompt_tokens=3))
    body = {**EMBED, "billed_to": {"user_id": "user-1", "org_id": "test-org", "role": role}}
    assert api.post("/v1/inference", json=body).status_code == status
    assert len(openai_fake.requests) == (status == 200)


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
    assert resp.json() == {"spent_usd": 1.25, "limit_usd": 5.0, "resets_at": "2026-10-14T00:00:00Z"}
    teacher = api.get("/v1/usage", params={"user_id": "teacher-1", "org_id": "test-org", "role": "teacher"})
    assert teacher.json()["spent_usd"] == 0.0 and teacher.json()["limit_usd"] == 10.0


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
        bill = billing.StreamBill(bills, billed, "corr", "gpt-test", PROMPT, 20)

        async def finish() -> None:
            await bill.close()
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


class SlowLedger(MemoryLedger):
    """Writes take a while, as over a network to Postgres."""

    def __init__(self, delay: float = 0.2) -> None:
        super().__init__()
        self.delay = delay

    async def record(self, billed: spend.Billed, charge: spend.Charge, correlation_id: str) -> None:
        await asyncio.sleep(self.delay)
        await super().record(billed, charge, correlation_id)


def a_dollar_an_embedding(monkeypatch: pytest.MonkeyPatch, test_settings: settings.Settings) -> None:
    """1000 prompt tokens of the embedding model cost $1."""
    priced = configured(model_prices={test_settings.embedding_model: (1000.0, 1000.0, 0.0)})
    monkeypatch.setattr("spend.get_settings", lambda: priced)


def test_the_next_call_sees_the_last_ones_charge(
    openai_fake: FakeOpenAI,
    test_settings: settings.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The charge is written before the response ends, so a caller's next call is checked against it."""
    from conftest import SERVICE_CLAIMS
    from learning_transport.testing import as_caller

    from main import app

    app.state.ledger = slow = SlowLedger()
    a_dollar_an_embedding(monkeypatch, test_settings)
    slow.already["student-1"] = 4.5
    openai_fake.reply(json=embedding_response([[0.5]], prompt_tokens=1000), times=2)
    with as_caller(app, SERVICE_CLAIMS), TestClient(app) as api:
        assert api.post("/v1/inference", json=EMBED).status_code == 200
        resp = api.post("/v1/inference", json=EMBED)
    assert (resp.status_code, resp.json()["error"]["code"]) == (429, "AI_SPEND_LIMIT")
    assert len(openai_fake.requests) == 1 and len(slow.charges) == 1


def test_overshoot_is_bounded_by_the_calls_in_flight(
    openai_fake: FakeOpenAI,
    test_settings: settings.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calls already past the check when the allowance runs out still finish and are charged; once they are
    recorded, nothing more runs. The overshoot is at most what the in-flight calls cost (ADR 0003)."""
    from conftest import SERVICE_CLAIMS
    from learning_transport.testing import as_caller

    from main import app

    app.state.ledger = slow = SlowLedger()
    a_dollar_an_embedding(monkeypatch, test_settings)
    slow.already["student-1"] = 4.5
    in_flight = 3
    openai_fake.reply(json=embedding_response([[0.5]], prompt_tokens=1000), times=in_flight + 1)

    async def burst() -> list[int]:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai-core") as client:
            responses = await asyncio.gather(
                *(client.post("/v1/inference", json=EMBED) for _ in range(in_flight))
            )
            after = await client.post("/v1/inference", json=EMBED)
        return [r.status_code for r in responses] + [after.status_code]

    monkeypatch.setattr(app.state, "billing", billing.Billing(slow), raising=False)
    with as_caller(app, SERVICE_CLAIMS):
        statuses = asyncio.run(burst())
    assert statuses == [200] * in_flight + [429]
    spent = 4.5 + sum(c.cost_usd for _, c in slow.charges)
    assert spent == pytest.approx(4.5 + in_flight * 1.0) and len(openai_fake.requests) == in_flight


def test_a_users_running_calls_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("billing.get_settings", lambda: configured(user_concurrency=2))
    bills = billing.Billing(MemoryLedger())
    student = spend.Billed.model_validate(BILLED)
    other = spend.Billed.model_validate({**BILLED, "user_id": "student-2"})
    first, _ = bills.claim(student), bills.claim(student)
    with pytest.raises(HTTPException) as busy:
        bills.claim(student)
    assert busy.value.status_code == 429 and busy.value.detail["code"] == "AI_BUSY"  # type: ignore[index]
    bills.claim(other).release()  # another user is not held up
    first.release()
    first.release()  # releasing twice frees one slot, not two
    bills.claim(student)
    with pytest.raises(HTTPException):
        bills.claim(student)


def test_calls_beyond_the_cap_are_busy_until_one_ends(
    openai_fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call holds its slot until its charge is written, so a burst runs at most AI_USER_CONCURRENCY calls."""
    from conftest import SERVICE_CLAIMS
    from learning_transport.testing import as_caller

    from main import app

    monkeypatch.setattr("billing.get_settings", lambda: configured(user_concurrency=2))
    slow = SlowLedger()
    monkeypatch.setattr(app.state, "billing", billing.Billing(slow), raising=False)
    openai_fake.reply(json=embedding_response([[0.5]], prompt_tokens=10), times=3)

    async def burst() -> tuple[list[tuple[int, str]], int]:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai-core") as client:
            responses = await asyncio.gather(*(client.post("/v1/inference", json=EMBED) for _ in range(4)))
            after = await client.post("/v1/inference", json=EMBED)
        codes = [(r.status_code, r.json().get("error", {}).get("code", "")) for r in responses]
        return codes, after.status_code

    with as_caller(app, SERVICE_CLAIMS):
        codes, after = asyncio.run(burst())
    assert sorted(codes) == [(200, ""), (200, ""), (429, "AI_BUSY"), (429, "AI_BUSY")]
    assert after == 200 and len(openai_fake.requests) == 3


@pytest.mark.parametrize("kind", ["embed", "stream", "rerank"])
def test_every_call_frees_its_slot_however_it_ends(
    api: TestClient, openai_fake: FakeOpenAI, reranker_fake: FakeReranker, ledger: MemoryLedger, kind: str
) -> None:
    from main import app

    running = app.state.billing._running

    def call(succeeds: bool) -> int:
        if kind == "rerank":
            scores = {"data": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]}
            reranker_fake.handler = (
                lambda r: httpx2.Response(200, json=scores) if succeeds else httpx2.Response(500)
            )
            return api.post("/v1/rerank", json=RERANK).status_code
        if not succeeds:
            openai_fake.reply(500, json={"error": {"message": "down"}}, times=10)
        elif kind == "stream":
            openai_fake.replies.append(chat_stream(["Hi"]))
        else:
            openai_fake.reply(json=embedding_response([[0.5]], prompt_tokens=3))
        body = {**GENERATE, "stream": True} if kind == "stream" else EMBED
        return api.post("/v1/inference", json=body).status_code

    assert call(True) == 200 and running == {}
    ledger.already["student-1"] = 5.0  # refused by the allowance check, before the provider
    assert call(True) == 429 and running == {}
    ledger.already.clear()
    openai_fake.replies.clear()
    assert call(False) == 502 and running == {}


def test_a_failed_ledger_write_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The money is already spent: the call still succeeds, and the lost charge is in the log for reconciling."""

    class Unwritable(MemoryLedger):
        async def record(self, billed: spend.Billed, charge: spend.Charge, correlation_id: str) -> None:
            raise ConnectionError("ledger down")

    charge = spend.Charge("embed", "text-embedding-3-small", 10, 0, 0.25)
    with caplog.at_level("ERROR", logger="ai-core.billing"):
        asyncio.run(billing.Billing(Unwritable()).charge(spend.Billed.model_validate(BILLED), "corr", charge))
    [record] = [r for r in caplog.records if r.name == "ai-core.billing"]
    assert record.getMessage() == "ledger_write_failed"
    assert (record.__dict__["operation"], record.__dict__["cost_usd"]) == ("embed", 0.25)


def test_an_unpriced_reranker_is_charged_the_highest_rates(
    api: TestClient,
    reranker_fake: FakeReranker,
    ledger: MemoryLedger,
    test_settings: settings.Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert test_settings.reranker_model not in spend.prices()
    reranker_fake.handler = lambda r: httpx2.Response(
        200,
        json={
            "data": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}],
            "usage": {"total_tokens": 40},
        },
    )
    with caplog.at_level("WARNING", logger="ai-core.spend"):
        assert api.post("/v1/rerank", json=RERANK).status_code == 200
    [(_, charge)] = ledger.charges
    highest = max(p[0] for p in spend.prices().values())
    assert charge.model == test_settings.reranker_model and charge.cost_usd > 0
    assert charge.cost_usd == pytest.approx(40 * highest / 1_000_000)
    assert "unpriced_model" in [r.getMessage() for r in caplog.records]
