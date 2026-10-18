import asyncio
import json
from typing import Any

import httpx
import pytest

from learning_transport import Claims, UpstreamError
from learning_transport.agents import AGENT_ENV, AgentClients, AgentSettings, billed_to, read_settings
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
        self.inference: list[httpx.Response] = []
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
            return self.inference.pop(0)
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
