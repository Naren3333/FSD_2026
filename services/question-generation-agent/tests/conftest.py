"""fakes for the agent's tests: identity, curriculum, content, ai-core and keycloak's token endpoint behind one httpx mocktransport."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from learning_transport.testing import as_caller, token_claims

import main
from clients import Services
from config import Settings

ROOM = "00000000-0000-4000-8000-000000000001"
TOPIC = "00000000-0000-4000-8000-0000000000a1"
DOC = "00000000-0000-4000-8000-0000000000d1"
CHUNK_1 = "00000000-0000-4000-8000-0000000000c1"
CHUNK_2 = "00000000-0000-4000-8000-0000000000c2"
SETTINGS = Settings(
    identity_url="http://identity:8080",
    curriculum_url="http://curriculum:8080",
    content_url="http://content:8080",
    ai_core_url="http://ai-core:8080",
    token_url="http://keycloak:8080/token",
    client_id="question-generation-agent",
    client_secret="secret",
)

UNIT_TESTS = (
    "A unit test checks a single function or class in isolation. Its dependencies, such as the database or a "
    "payment service, are replaced with test doubles so a failure points at the unit itself."
)
BOUNDARIES = (
    "Boundary value analysis tests the values at the edges of each input range. For a range of 18 to 65 "
    "inclusive, test 17, 18, 65 and 66, because off-by-one mistakes happen at the edges."
)


def hit(chunk: str, content: str, score: float = 0.8, page: int = 3) -> dict[str, Any]:
    return {"chunk_id": chunk, "document_id": DOC, "title": "Testing slides", "doc_type": "teaching_material",
            "section_path": None, "page_start": page, "page_end": page, "content": content, "kind": "text",
            "source": "pdf", "image": None, "score": score, "related": []}  # fmt: skip


def reply(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, json={"output": json.dumps(body), "model_version": "gpt-test", "usage_tokens": 9}
    )


class Fakes:
    """what the other services answer."""

    def __init__(self) -> None:
        self.teachers = {("Bearer t1", ROOM)}
        self.students = {("Bearer s1", ROOM)}
        self.topic: dict[str, Any] | None = {"id": TOPIC, "name": "Software testing"}
        self.results = [hit(CHUNK_1, UNIT_TESTS), hit(CHUNK_2, BOUNDARIES, 0.7, 5)]
        self.fallback = False
        self.topic_status = 200
        self.search_body: dict[str, Any] | None = None  # replaces the search reply when set
        self.search_status = 200
        self.search_error: dict[str, Any] = {"error": {"code": "SEARCH_UNAVAILABLE", "message": "x"}}
        self.replies: list[httpx.Response] = []
        self.requests: list[tuple[str, dict[str, Any], httpx.Request]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        is_json = request.headers.get("content-type") == "application/json"
        body = json.loads(request.content) if request.method == "POST" and is_json else {}
        self.requests.append((path, body, request))
        token = request.headers.get("authorization")
        if path == "/token":
            return httpx.Response(200, json={"access_token": "service-token", "expires_in": 300})
        if path.endswith("/access"):
            classroom = path.split("/")[3]
            if (token, classroom) in self.teachers:
                return httpx.Response(200, json={"allowed": True, "relation": "teacher"})
            if (token, classroom) in self.students:
                return httpx.Response(200, json={"allowed": True, "relation": "student"})
            return httpx.Response(403, json={"error": {"code": "FORBIDDEN", "message": "denied"}})
        if path.startswith("/v1/topics/"):
            if self.topic_status != 200:
                return httpx.Response(self.topic_status)
            if self.topic is None:
                return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})
            return httpx.Response(200, json=self.topic)
        if path == "/v1/search":
            if self.search_status != 200:
                return httpx.Response(self.search_status, json=self.search_error)
            if self.search_body is not None:
                return httpx.Response(200, json=self.search_body)
            return httpx.Response(200, json={"results": self.results, "rerank_fallback": self.fallback})
        if path == "/v1/inference":
            assert token == "Bearer service-token", "ai-core must get the agent's token"
            return self.replies.pop(0)
        return httpx.Response(404)

    def calls(self, path: str) -> list[dict[str, Any]]:
        return [body for p, body, _ in self.requests if p == path]

    def headers(self, path: str) -> list[httpx.Headers]:
        return [r.headers for p, _, r in self.requests if p == path]


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def api(fakes: Fakes) -> Iterator[TestClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fakes.handle))
    main.app.state.settings = SETTINGS
    main.app.state.services = Services(SETTINGS, http)
    with as_caller(main.app, fake_actor):
        yield TestClient(main.app)


def fake_actor(request: Request) -> dict[str, Any]:
    """verified claims for the user named in the test's authorization header: t… is a teacher, a… a service account, anyone else a student."""
    user = request.headers["authorization"].removeprefix("Bearer ")
    role = "teacher" if user.startswith("t") else "ai-client" if user.startswith("a") else "student"
    return token_claims(role, sub=user, org="org")


def as_user(user: str = "t1") -> dict[str, str]:
    return {"Authorization": f"Bearer {user}"}
