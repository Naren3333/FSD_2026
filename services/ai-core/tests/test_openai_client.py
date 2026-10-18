"""openai_client: embeddings, chat (plain, streamed, JSON schema) and the retry rules, against a mock OpenAI.
Ported from fsd_test's shared tests."""

from collections.abc import Callable

import httpx2
import openai
import pytest
from conftest import CACHED_USAGE, FakeOpenAI, chat_response, chat_stream, configured, embedding_response

import openai_client
from openai_client import StructuredOutputError, Usage

# Async tests run on anyio's pytest plugin (installed with FastAPI), on asyncio (conftest.anyio_backend).
pytestmark = pytest.mark.anyio

RATE_LIMITED = {"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}}
QUOTA = {
    "error": {
        "message": "You exceeded your current quota",
        "type": "insufficient_quota",
        "code": "insufficient_quota",
    }
}
BOOM = {"error": {"message": "boom"}}
OK = embedding_response([[0.5, 0.25]])


async def test_vectors_follow_index_not_data_order(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=embedding_response([[1.0], [2.0], [3.0]], order=[2, 0, 1]))
    assert await openai_client.embed(["a", "b", "c"]) == ([[1.0], [2.0], [3.0]], 8)
    [req] = openai_fake.requests
    assert req["input"] == ["a", "b", "c"] and req["model"] == "text-embedding-3-small"
    assert openai_fake.timeouts == [30]


async def test_batches_of_128_sent_in_order(openai_fake: FakeOpenAI) -> None:
    texts = [f"t{i}" for i in range(300)]
    for start, size in [(0, 128), (128, 128), (256, 44)]:
        openai_fake.reply(json=embedding_response([[float(start + i)] for i in range(size)]))
    vectors, tokens = await openai_client.embed(texts)
    assert tokens == 24
    assert [len(r["input"]) for r in openai_fake.requests] == [128, 128, 44]
    assert openai_fake.requests[2]["input"][0] == "t256"
    assert vectors == [[float(i)] for i in range(300)]


async def test_the_tally_counts_batches_answered_before_a_failure(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=embedding_response([[0.0]] * 128))
    openai_fake.reply(400, json=BOOM)
    tally = openai_client.Tally()
    with pytest.raises(openai.BadRequestError):
        await openai_client.embed(["t"] * 200, tally)
    assert tally.tokens == 8


async def test_empty_input_makes_no_request(openai_fake: FakeOpenAI) -> None:
    assert await openai_client.embed([]) == ([], 0)
    assert openai_fake.requests == []


async def test_429_twice_then_success(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(429, json=RATE_LIMITED, times=2)
    openai_fake.reply(json=OK)
    assert await openai_client.embed(["a"]) == ([[0.5, 0.25]], 8)
    assert len(openai_fake.requests) == 3 and len(openai_fake.sleeps) == 2
    assert all(0 <= d <= 8 for d in openai_fake.sleeps)  # full jitter


async def test_three_500s_raise_after_three_attempts(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(500, json=BOOM, times=3)
    with pytest.raises(openai.InternalServerError):
        await openai_client.embed(["a"])
    assert len(openai_fake.requests) == 3


async def test_retry_after_ms_is_honoured(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(429, json=RATE_LIMITED, headers={"retry-after-ms": "1500"})
    openai_fake.reply(json=OK)
    await openai_client.embed(["a"])
    assert openai_fake.sleeps == [1.5]


async def test_long_retry_after_fails_at_once(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(429, json=RATE_LIMITED, headers={"retry-after": "30"})
    with pytest.raises(openai.RateLimitError):
        await openai_client.embed(["a"])
    assert len(openai_fake.requests) == 1 and openai_fake.sleeps == []


async def test_insufficient_quota_is_not_retried(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(429, json=QUOTA)
    with pytest.raises(openai.RateLimitError, match="quota"):
        await openai_client.embed(["a"])
    assert len(openai_fake.requests) == 1


async def test_client_errors_are_not_retried(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(400, json={"error": {"message": "Unsupported value"}})
    with pytest.raises(openai.BadRequestError):
        await openai_client.embed(["a"])
    assert len(openai_fake.requests) == 1


async def test_connection_error_is_retried(openai_fake: FakeOpenAI) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    openai_fake.replies += [refuse, httpx2.Response(200, json=OK)]
    assert await openai_client.embed(["a"]) == ([[0.5, 0.25]], 8)
    assert len(openai_fake.requests) == 2


async def test_timeout_is_not_retried(openai_fake: FakeOpenAI) -> None:
    def hang(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    openai_fake.replies.append(hang)
    with pytest.raises(openai.APITimeoutError):
        await openai_client.embed(["a"])
    assert len(openai_fake.requests) == 1 and openai_fake.sleeps == []


def test_missing_key_raises_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_client, "_client", None)
    monkeypatch.setattr(openai_client, "get_settings", lambda: configured(openai_api_key=None))
    with pytest.raises(openai_client.NotConfiguredError):
        openai_client.get_client()


# ---- stream_chat

HELLO = [{"role": "user", "content": "hi"}]


async def collect(max_tokens: int = 50) -> list[str | Usage]:
    return [item async for item in openai_client.stream_chat(HELLO, max_tokens)]


async def test_stream_relays_tokens_in_order_then_usage(openai_fake: FakeOpenAI) -> None:
    openai_fake.replies.append(chat_stream(["Photo", "synthesis", " [S1]"]))
    assert await collect() == ["Photo", "synthesis", " [S1]", Usage(120, 7)]
    [req] = openai_fake.requests
    assert req["model"] == "gpt-test" and req["reasoning_effort"] == "none"
    assert req["stream"] is True and req["stream_options"] == {"include_usage": True}
    assert req["max_completion_tokens"] == 50 and req["messages"] == HELLO
    assert openai_fake.timeouts == [30]


async def test_stream_usage_includes_cached_prompt_tokens(openai_fake: FakeOpenAI) -> None:
    openai_fake.replies.append(chat_stream(["a"], usage=CACHED_USAGE))
    assert await collect() == ["a", Usage(120, 7, cached_in=100)]


async def test_stream_skips_empty_deltas(openai_fake: FakeOpenAI) -> None:
    openai_fake.replies.append(chat_stream(["a", "", "b"]))
    assert await collect() == ["a", "b", Usage(120, 7)]


async def test_stream_without_usage_chunk_yields_empty_usage(openai_fake: FakeOpenAI) -> None:
    openai_fake.replies.append(chat_stream(["a"], usage=None))
    assert await collect() == ["a", Usage(None, None)]


async def test_stream_is_retried_before_the_first_token(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(429, json=RATE_LIMITED)
    openai_fake.replies.append(chat_stream(["ok"]))
    assert await collect() == ["ok", Usage(120, 7)]
    assert len(openai_fake.requests) == 2 and len(openai_fake.sleeps) == 1


async def test_stream_is_not_retried_after_the_first_token(openai_fake: FakeOpenAI) -> None:
    openai_fake.replies.append(chat_stream(["one", "two", "three"], fail_after=2))
    got: list = []
    with pytest.raises(openai.APIConnectionError):
        async for item in openai_client.stream_chat(HELLO, 50):
            got.append(item)
    assert got == ["one", "two"]
    assert len(openai_fake.requests) == 1 and openai_fake.sleeps == []


async def test_breaking_out_early_closes_the_stream(openai_fake: FakeOpenAI) -> None:
    response = chat_stream(["a", "b", "c"])
    openai_fake.replies.append(response)
    stream = openai_client.stream_chat(HELLO, 50)
    async for item in stream:
        assert item == "a"
        break
    assert not response.is_closed
    await stream.aclose()  # as when the pipeline's generator is closed on a client disconnect
    assert response.is_closed


# ---- generate


async def test_reply_usage_includes_cached_prompt_tokens(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=chat_response("Hi", usage=CACHED_USAGE))
    reply = await openai_client.generate(HELLO, 40)
    assert reply.usage == Usage(120, 7, cached_in=100)


REWRITE = '{"query": "stages of photosynthesis", "is_on_topic": true}'
SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "is_on_topic": {"type": "boolean"}},
    "required": ["query", "is_on_topic"],
    "additionalProperties": False,
}


async def rewrite() -> openai_client.Completion:
    return await openai_client.generate(HELLO, 100, SCHEMA, name="query_rewrite.v1")


async def test_plain_reply(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=chat_response("Hello there"))
    reply = await openai_client.generate(HELLO, 40)
    assert reply == openai_client.Completion("Hello there", Usage(120, 7), "stop")
    [req] = openai_fake.requests
    assert "response_format" not in req and req["max_completion_tokens"] == 40


async def test_schema_reply_is_returned_as_json_text(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=chat_response(REWRITE))
    assert (await rewrite()).text == REWRITE
    [req] = openai_fake.requests
    schema = req["response_format"]["json_schema"]
    assert req["response_format"]["type"] == "json_schema"
    assert schema["name"] == "query_rewrite_v1" and schema["strict"] is True and schema["schema"] == SCHEMA
    assert req["max_completion_tokens"] == 100 and req["reasoning_effort"] == "none"


@pytest.mark.parametrize(
    "reply, match",
    [
        (chat_response(None, refusal="I can't help with that"), "refused"),
        (chat_response('{"query": "stag', finish_reason="length"), "length"),
        (chat_response('{"query": "x"}', finish_reason="content_filter"), "content_filter"),
    ],
)
async def test_schema_failures_raise_and_are_not_retried(
    openai_fake: FakeOpenAI, reply: dict, match: str
) -> None:
    openai_fake.reply(json=reply)
    with pytest.raises(StructuredOutputError, match=match) as error:
        await rewrite()
    assert len(openai_fake.requests) == 1 and openai_fake.sleeps == []
    assert error.value.usage == Usage(120, 7)  # billed by OpenAI all the same: the caller charges it


async def test_generate_500_is_retried(openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(500, json=BOOM)
    openai_fake.reply(json=chat_response(REWRITE))
    assert (await rewrite()).text == REWRITE
    assert len(openai_fake.requests) == 2


class Clock:
    """openai_client's monotonic clock, moved by hand; each recorded retry sleep advances it too."""

    def __init__(self, fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        monkeypatch.setattr(openai_client, "_now", lambda: self.now)

        async def sleep(delay: float) -> None:
            fake.sleeps.append(delay)
            self.now += delay

        monkeypatch.setattr(openai_client, "_sleep", sleep)

    def slow(self, seconds: float, status: int) -> Callable[[httpx2.Request], httpx2.Response]:
        """A reply that fails with `status` after `seconds`."""

        def reply(request: httpx2.Request) -> httpx2.Response:
            self.now += seconds
            return httpx2.Response(status, json=BOOM)

        return reply


def test_the_generation_budget_grows_with_max_tokens() -> None:
    budget = openai_client.generation_budget
    assert budget(300) == openai_client.TIMEOUT  # never below the per-read timeout
    assert budget(1500) == pytest.approx(45)  # Grading's marking call
    assert budget(6000) == pytest.approx(120)  # question generation's longest reply
    assert budget(8000) == openai_client.GENERATE_CAP  # the most the contract allows


async def test_a_long_reply_gets_its_whole_budget(
    openai_fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    Clock(openai_fake, monkeypatch)
    openai_fake.reply(json=chat_response(REWRITE))
    await openai_client.generate(HELLO, 6000, SCHEMA, name="questions.generated")
    assert openai_fake.timeouts == [pytest.approx(120)]


async def test_a_retry_gets_what_is_left_of_the_budget(
    openai_fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(openai_fake, monkeypatch)
    openai_fake.replies += [clock.slow(50, 500), httpx2.Response(200, json=chat_response(REWRITE))]
    await openai_client.generate(HELLO, 6000, SCHEMA, name="questions.generated")
    first, second = openai_fake.timeouts
    assert first == pytest.approx(120) and second == pytest.approx(70 - openai_fake.sleeps[0])


async def test_no_retry_starts_without_enough_budget_left(
    openai_fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(openai_fake, monkeypatch)
    openai_fake.replies.append(clock.slow(115, 500))
    with pytest.raises(openai.InternalServerError):
        await openai_client.generate(HELLO, 6000, SCHEMA, name="questions.generated")
    assert len(openai_fake.requests) == 1 and openai_fake.sleeps == []


async def test_a_stream_is_not_reopened_once_a_whole_attempt_no_longer_fits(
    openai_fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(openai_fake, monkeypatch)
    openai_fake.replies.append(clock.slow(20, 503))
    with pytest.raises(openai.InternalServerError):
        await collect()
    # 20 s gone leaves 25 s of STREAM_OPEN_BUDGET: less than one more 30 s attempt.
    assert len(openai_fake.requests) == 1 and openai_fake.sleeps == []
