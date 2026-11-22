import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from learning_transport import Claims, UpstreamError
from learning_transport.agents import (
    AGENT_ENV,
    MIN_CALL,
    OUT_OF_TIME,
    REPLY_MARGIN,
    AgentClients,
    AgentSettings,
    CheckFailedError,
    billed_to,
    gather_all,
    read_settings,
)
from learning_transport.testing import token_claims

SETTINGS = AgentSettings(
    identity_url="http://identity:8080",
    content_url="http://content:8080",
    ai_core_url="http://ai-core:8080",
    token_url="http://keycloak:8080/token",
    client_id="agent",
    client_secret="secret",
)
HIT = {"chunk_id": "c1", "document_id": "d1", "content": "Chlorophyll absorbs light.", "score": 0.8}


class Fakes:
    def __init__(self) -> None:
        self.search: httpx.Response = httpx.Response(200, json={"results": [HIT], "rerank_fallback": False})
        self.inference: list[httpx.Response | Callable[[httpx.Request], httpx.Response]] = []
        self.requests: list[httpx.Request] = []
        self.tokens = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/token":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"service-{self.tokens}", "expires_in": 300})
        if request.url.path == "/v1/search":
            return self.search
        if request.url.path == "/v1/inference":
            reply = self.inference.pop(0)
            return reply(request) if callable(reply) else reply
        return httpx.Response(404)

    def clients(self) -> AgentClients:
        return AgentClients(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(self.handle)))


def generate(clients: AgentClients) -> str:
    billed = {"user_id": "u1", "org_id": "o1", "role": "teacher"}
    return asyncio.run(clients.generate([{"role": "user", "content": "q"}], 50, {}, "x", "corr", billed))


def test_search_forwards_the_users_token_and_returns_valid_hits() -> None:
    fakes = Fakes()
    body = {"classroom_id": "r1", "query": "light"}
    results, fallback = asyncio.run(fakes.clients().search_content("Bearer user", body, "corr", 5))
    assert (results, fallback) == ([HIT], False)
    request = fakes.requests[0]
    assert request.headers["authorization"] == "Bearer user"
    assert request.headers["x-correlation-id"] == "corr"


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"rerank_fallback": False}),
        httpx.Response(200, json={"results": {"chunk_id": "c1"}}),
        httpx.Response(200, json={"results": [{**HIT, "content": None}]}),
        httpx.Response(200, json={"results": [{**HIT, "score": "high"}]}),
        httpx.Response(200, json=[HIT]),
    ],
    ids=["not-json", "no-results", "not-a-list", "no-content", "bad-score", "array"],
)
def test_a_malformed_search_reply_is_a_bad_gateway(reply: httpx.Response) -> None:
    fakes = Fakes()
    fakes.search = reply
    with pytest.raises(UpstreamError) as failed:
        asyncio.run(fakes.clients().search_content("Bearer user", {}, "corr", 5))
    assert (failed.value.service, failed.value.status) == ("content", 502)


def test_generate_sends_the_agents_token_and_the_billed_user() -> None:
    fakes = Fakes()
    fakes.inference = [httpx.Response(200, json={"output": '{"a": 1}'})]
    assert generate(fakes.clients()) == '{"a": 1}'
    request = fakes.requests[-1]
    assert request.headers["authorization"] == "Bearer service-1"
    body: dict[str, Any] = json.loads(request.content)
    assert body["billed_to"] == {"user_id": "u1", "org_id": "o1", "role": "teacher"}
    assert body["operation"] == "generate" and body["schema_name"] == "x"


@pytest.mark.parametrize("reply", [{"output": None}, {"output": 3}, ["x"]])
def test_generate_rejects_an_output_that_is_not_text(reply: object) -> None:
    fakes = Fakes()
    fakes.inference = [httpx.Response(200, json=reply)]
    with pytest.raises(UpstreamError) as failed:
        generate(fakes.clients())
    assert (failed.value.status, failed.value.code) == (502, "AI_OUTPUT_REJECTED")


def test_a_refused_service_token_is_replaced_on_the_next_call() -> None:
    fakes = Fakes()
    clients = fakes.clients()
    fakes.inference = [
        httpx.Response(401, json={"error": {"code": "UNAUTHENTICATED", "message": "x"}}),
        httpx.Response(200, json={"output": "ok"}),
    ]
    with pytest.raises(UpstreamError):
        generate(clients)
    assert generate(clients) == "ok"
    tokens = [r.headers["authorization"] for r in fakes.requests if r.url.path == "/v1/inference"]
    assert tokens == ["Bearer service-1", "Bearer service-2"]


def test_billed_to_names_the_user_and_the_allowance() -> None:
    claims = Claims.from_token(token_claims("teacher", sub="u1", org="o1"))
    assert billed_to(claims, "student") == {"user_id": "u1", "org_id": "o1", "role": "student"}


def test_read_settings_strips_trailing_slashes_and_requires_every_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in AGENT_ENV.values():
        monkeypatch.setenv(name, f"http://{name.lower()}/")
    values = read_settings(AGENT_ENV)
    assert AgentSettings(**values).content_url == "http://content_url"
    monkeypatch.delenv("AI_CLIENT_SECRET")
    with pytest.raises(RuntimeError, match="AI_CLIENT_SECRET"):
        read_settings(AGENT_ENV)


def test_generate_tells_ai_core_to_give_up_before_the_agent_does() -> None:
    fakes = Fakes()
    fakes.inference = [httpx.Response(200, json={"output": "ok"})]
    generate(fakes.clients())
    body = json.loads(fakes.requests[-1].content)
    assert body["timeout"] == AgentClients.generate_timeout - REPLY_MARGIN


def test_a_call_the_deadline_cut_off_is_out_of_time() -> None:
    fakes = Fakes()
    clients = fakes.clients()
    fakes.inference = [httpx.Response(504, json={"error": {"code": "AI_TIMEOUT", "message": "x"}})]
    billed = {"user_id": "u1", "org_id": "o1", "role": "teacher"}
    deadline = time.monotonic() + 20

    def ask(until: float) -> str:
        return asyncio.run(
            clients.generate([{"role": "user", "content": "q"}], 50, {}, "x", "c", billed, until)
        )

    with pytest.raises(UpstreamError) as failed:
        ask(deadline)
    assert failed.value.code == OUT_OF_TIME
    assert json.loads(fakes.requests[-1].content)["timeout"] <= 20 - REPLY_MARGIN
    calls = len(fakes.requests)
    with pytest.raises(UpstreamError) as failed:
        ask(time.monotonic() + MIN_CALL - 1)  # too little time left: not started
    assert failed.value.code == OUT_OF_TIME and len(fakes.requests) == calls


def test_an_ai_core_timeout_without_a_deadline_stays_a_timeout() -> None:
    fakes = Fakes()
    fakes.inference = [httpx.Response(504, json={"error": {"code": "AI_TIMEOUT", "message": "x"}})]
    with pytest.raises(UpstreamError) as failed:
        generate(fakes.clients())
    assert failed.value.code == "AI_TIMEOUT"


def test_gather_all_cancels_the_rest_when_one_fails() -> None:
    cancelled = []

    async def slow() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def fails() -> None:
        raise UpstreamError("grading", 503)

    async def ok() -> int:
        return 1

    with pytest.raises(UpstreamError):
        asyncio.run(gather_all(slow(), fails()))
    assert cancelled == [True]
    assert asyncio.run(gather_all(ok(), ok())) == [1, 1]


# ---- profiles and the re-ask (adr 0023)

PAYER = {"user_id": "u1", "org_id": "o1", "role": "teacher"}
QUESTION = [{"role": "user", "content": "q"}]
REJECTED = httpx.Response(502, json={"error": {"code": "AI_OUTPUT_REJECTED", "message": "x"}})


def ask(clients: AgentClients, **options: Any) -> str:
    return asyncio.run(clients.generate(QUESTION, 50, {}, "x", "corr", PAYER, **options))


def inference_bodies(fakes: Fakes) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in fakes.requests if r.url.path == "/v1/inference"]


def test_generate_names_its_model_profile() -> None:
    fakes = Fakes()
    fakes.inference = [httpx.Response(200, json={"output": "a"}), httpx.Response(200, json={"output": "b"})]
    clients = fakes.clients()
    assert (ask(clients), ask(clients, profile="check")) == ("a", "b")
    assert [b["model_profile"] for b in inference_bodies(fakes)] == ["default", "check"]
    with pytest.raises(ValueError, match="profile"):
        ask(clients, profile="fastest")


def test_generate_sends_its_own_reasoning_effort_only_when_given_and_on_every_re_ask() -> None:
    fakes = Fakes()
    fakes.inference = [
        httpx.Response(200, json={"output": "a"}),
        REJECTED,
        httpx.Response(200, json={"output": "bad"}),
        httpx.Response(200, json={"output": "good"}),
    ]
    clients = fakes.clients()
    assert ask(clients) == "a"
    check = lambda output: ["faulty"] if output == "bad" else []  # noqa: E731
    assert ask(clients, profile="check", reasoning_effort="low", check=check, reask=2) == "good"
    plain, *asked = inference_bodies(fakes)
    assert "reasoning_effort" not in plain and len(asked) == 3
    assert all((b["model_profile"], b["reasoning_effort"]) == ("check", "low") for b in asked)
    with pytest.raises(ValueError, match="effort"):
        ask(clients, reasoning_effort="high")


def test_a_reply_the_check_faults_is_asked_again_with_the_faults() -> None:
    fakes = Fakes()
    fakes.inference = [
        httpx.Response(200, json={"output": "bad"}),
        httpx.Response(200, json={"output": "good"}),
    ]
    seen: list[str] = []

    def check(output: str) -> list[str]:
        seen.append(output)
        return ["quote q3 is not in the passage"] if output == "bad" else []

    assert ask(fakes.clients(), check=check, reask=1) == "good"
    assert seen == ["bad", "good"]
    first, second = inference_bodies(fakes)
    assert second["billed_to"] == first["billed_to"] == PAYER
    assert second["messages"][:2] == [*QUESTION, {"role": "assistant", "content": "bad"}]
    follow_up = second["messages"][2]
    assert follow_up["role"] == "user" and "quote q3 is not in the passage" in follow_up["content"]


def test_a_rejected_reply_is_asked_again_without_a_previous_reply() -> None:
    fakes = Fakes()
    fakes.inference = [REJECTED, httpx.Response(200, json={"output": "good"})]
    assert ask(fakes.clients(), reask=1) == "good"
    second = inference_bodies(fakes)[1]["messages"]
    assert second[0] == QUESTION[0] and len(second) == 2 and second[1]["role"] == "user"


def test_a_reply_still_faulty_after_the_reask_raises_check_failed() -> None:
    fakes = Fakes()
    fakes.inference = [
        httpx.Response(200, json={"output": "bad"}),
        httpx.Response(200, json={"output": "worse"}),
    ]
    with pytest.raises(CheckFailedError) as failed:
        ask(fakes.clients(), check=lambda output: [f"{output} is wrong"], reask=1)
    assert (failed.value.code, failed.value.status) == ("AI_OUTPUT_REJECTED", 502)
    assert (failed.value.errors, failed.value.output) == (["worse is wrong"], "worse")
    assert len(inference_bodies(fakes)) == 2


def test_a_reply_rejected_twice_stays_rejected() -> None:
    fakes = Fakes()
    fakes.inference = [REJECTED, REJECTED]
    with pytest.raises(UpstreamError) as failed:
        ask(fakes.clients(), reask=1)
    assert failed.value.code == "AI_OUTPUT_REJECTED" and not isinstance(failed.value, CheckFailedError)
    assert len(inference_bodies(fakes)) == 2


def test_without_reask_a_failure_is_not_asked_again() -> None:
    fakes = Fakes()
    fakes.inference = [REJECTED]
    with pytest.raises(UpstreamError):
        ask(fakes.clients())
    fakes.inference = [httpx.Response(200, json={"output": "bad"})]
    with pytest.raises(CheckFailedError):
        ask(fakes.clients(), check=lambda output: ["no"])
    assert len(inference_bodies(fakes)) == 2


def test_no_reask_starts_without_time_left_before_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = Fakes()
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def slow(request: httpx.Request) -> httpx.Response:
        clock[0] += 16  # of 20 s: less than MIN_CALL is left for a re-ask
        return httpx.Response(200, json={"output": "bad"})

    fakes.inference = [slow]
    with pytest.raises(CheckFailedError) as failed:
        ask(fakes.clients(), check=lambda output: ["no"], reask=1, deadline=clock[0] + 20)
    assert failed.value.output == "bad" and len(inference_bodies(fakes)) == 1
