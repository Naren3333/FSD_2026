"""fakes for the chatbot's tests: an in-memory store, and identity, content, ai-core and keycloak's token endpoint behind one httpx MockTransport."""

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from learning_transport import sse_event, testing
from learning_transport.testing import as_caller

import main
import prompts
from clients import Services
from config import Settings

ROOM = "00000000-0000-4000-8000-000000000001"
OTHER_ROOM = "00000000-0000-4000-8000-000000000002"
SETTINGS = Settings(
    database_url="postgresql://unused",
    adaptive_rewrite=False,
    adaptive_search=False,
    context_tokens=60000,
    identity_url="http://identity:8080",
    content_url="http://content:8080",
    ai_core_url="http://ai-core:8080",
    token_url="http://keycloak:8080/token",
    client_id="question-chatbot-agent",
    client_secret="secret",
)


class MemoryStore:
    def __init__(self) -> None:
        self.threads: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.purged: set[str] = set()
        self.gone: set[str] = set()  # threads purged since a turn read them
        # user → the thread and claim of their answer in progress
        self.active: dict[str, tuple[str, datetime]] = {}

    async def create_thread(self, org: str, user: str, role: str, classroom_id: str) -> dict[str, Any] | None:
        if classroom_id in self.purged:
            return None
        now = datetime.now(UTC)
        thread = {"id": str(uuid4()), "org": org, "user": user, "role": role, "classroom_id": classroom_id,
                  "title": None, "created_at": now, "last_message_at": now}  # fmt: skip
        self.threads[thread["id"]] = thread
        return self._public(thread)

    def _public(self, t: dict[str, Any]) -> dict[str, Any]:
        return {k: t[k] for k in ("id", "classroom_id", "role", "title", "created_at", "last_message_at")}

    async def list_threads(self, org: str, user: str, classroom_id: str | None) -> list[dict[str, Any]]:
        return [self._public(t) for t in self.threads.values() if (t["org"], t["user"]) == (org, user)
                and classroom_id in (None, t["classroom_id"])]  # fmt: skip

    async def thread(self, org: str, user: str, thread_id: str) -> dict[str, Any] | None:
        t = self.threads.get(thread_id)
        return self._public(t) if t and (t["org"], t["user"]) == (org, user) else None

    def all(self, thread_id: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["thread_id"] == thread_id]

    async def messages(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        keys = ("id", "role", "content", "outcome", "citations", "created_at")  # what the api returns
        return [{k: r.get(k) for k in keys} for r in self.all(thread_id)][-limit:]

    async def claim_turn(self, thread_id: str) -> datetime | None:
        claim = datetime.now(UTC)
        if thread_id in self.gone:
            return claim
        user = self.threads[thread_id]["user"]
        if user in self.active:
            return None
        self.active[user] = (thread_id, claim)
        return claim

    async def release_turn(self, thread_id: str, claim: datetime) -> None:
        self.active = {u: c for u, c in self.active.items() if c != (thread_id, claim)}

    async def start_turn(
        self, thread_id: str, content: str, history_turns: int
    ) -> list[dict[str, str]] | None:
        if thread_id in self.gone:
            return None
        history = [{"role": r["role"], "content": r["content"]} for r in self.all(thread_id)][-history_turns:]
        self.rows.append({"id": str(uuid4()), "thread_id": thread_id, "role": "user", "content": content,
                          "outcome": None, "citations": None, "created_at": datetime.now(UTC)})  # fmt: skip
        if self.threads[thread_id]["title"] is None:
            self.threads[thread_id]["title"] = content[:80]
        return history

    async def save_answer(self, thread_id: str, answer: dict[str, Any], claim: datetime) -> str | None:
        if thread_id in self.gone:
            return None
        await self.release_turn(thread_id, claim)
        row = {"id": str(uuid4()), "thread_id": thread_id, "role": "assistant", "created_at": datetime.now(UTC),
               **answer}  # fmt: skip
        self.rows.append(row)
        return row["id"]

    async def save_check(self, message_id: str, answer_check: dict[str, Any], notice: str | None) -> bool:
        row = next((r for r in self.rows if r["id"] == message_id and r["role"] == "assistant"), None)
        if row is None or row["thread_id"] in self.gone:
            return False
        row["answer_check"], row["content"] = answer_check, row["content"] + (notice or "")
        return True


def chat_stream(deltas: list[str], fail: bool = False) -> str:
    events = [sse_event("delta", {"text": d}) for d in deltas]
    if fail:
        events.append(sse_event("error", {"code": "AI_PROVIDER_FAILED", "message": "x"}))
    else:
        events.append(sse_event("done", {"model_version": "gpt-test", "usage_tokens": 42}))
    return "".join(events)


def rewrite(
    query: str, on_topic: bool = True, small_talk: bool = False, sub_queries: list[str] | None = None
) -> httpx.Response:
    output = json.dumps(
        {
            "query": query,
            "is_on_topic": on_topic,
            "is_small_talk": small_talk,
            "sub_queries": sub_queries or [],
        }
    )
    return httpx.Response(200, json={"output": output, "model_version": "gpt-test", "usage_tokens": 9})


CHECK = "/v1/inference#answer_check"  # where Fakes records the support check's calls


def statement_ids(body: dict[str, Any]) -> list[str]:
    """the statement ids an answer_check.v1 call asks about."""
    return re.findall(r'<statement id="(\d+)"', body["messages"][-1]["content"])


def verdicts(body: dict[str, Any], *given: str) -> httpx.Response:
    """an answer_check.v1 reply: the given verdicts in statement order, the last one repeated for the rest."""
    ids = statement_ids(body)
    out = [{"id": i, "verdict": given[min(n, len(given) - 1)], "reason": "r"} for n, i in enumerate(ids)]
    return httpx.Response(
        200, json={"output": json.dumps({"verdicts": out}), "model_version": "gpt-check", "usage_tokens": 7}
    )


class Fakes:
    """what the other services answer. `replies` are ai-core's answers in order: a response, or a str for a stream."""

    def __init__(self) -> None:
        self.access = {("Bearer s1", ROOM), ("Bearer t1", ROOM)}
        self.results: list[dict[str, Any]] = []
        self.by_query: dict[str, list[dict[str, Any]]] = {}  # a search's results by its query; else `results`
        self.fallback = False
        # searches whose rerank fell back, besides all when `fallback`.
        self.fallback_queries: set[str] = set()
        self.search_status = 200
        self.search_error: dict[str, Any] = {"error": {"code": "X", "message": "x"}}
        self.replies: list[httpx.Response | str] = []
        # the support check's replies (answer_check.v1); when none is left, every statement is supported.
        self.checks: list[httpx.Response] = []
        self.requests: list[tuple[str, dict[str, Any], httpx.Request]] = []
        self.usage: dict[str, Any] = {
            "spent_usd": 1.5,
            "limit_usd": 5.0,
            "resets_at": "2026-10-14T00:00:00+08:00",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content and request.method == "POST" and \
            request.headers.get("content-type") == "application/json" else {}  # fmt: skip
        if path == "/v1/inference" and body.get("schema_name") == prompts.ANSWER_CHECK:
            # the support check has its own queue and path, so the other tests' replies and calls stay as they were.
            self.requests.append((CHECK, body, request))
            return self.checks.pop(0) if self.checks else verdicts(body, "supported")
        self.requests.append((path, body, request))
        if path == "/token":
            return httpx.Response(200, json={"access_token": "service-token", "expires_in": 300})
        if path.endswith("/access"):
            classroom = path.split("/")[3]
            token = request.headers["authorization"]
            if (token, classroom) not in self.access:
                return httpx.Response(403, json={"error": {"code": "FORBIDDEN", "message": "denied"}})
            relation = "teacher" if token == "Bearer t1" else "student"
            return httpx.Response(200, json={"allowed": True, "relation": relation})
        if path == "/v1/usage":
            return httpx.Response(200, json=self.usage)
        if path == "/v1/search":
            if self.search_status != 200:
                return httpx.Response(self.search_status, json=self.search_error)
            results = self.by_query.get(body["query"], self.results)
            fallback = self.fallback or body["query"] in self.fallback_queries
            return httpx.Response(200, json={"results": results, "rerank_fallback": fallback})
        if path == "/v1/inference":
            assert request.headers["authorization"] == "Bearer service-token", (
                "ai-core must get the agent's token"
            )
            reply = self.replies.pop(0)
            if isinstance(reply, str):
                return httpx.Response(200, text=reply, headers={"content-type": "text/event-stream"})
            return reply
        return httpx.Response(404)

    @staticmethod
    def reply_for(given: list[str]) -> httpx.Response:
        """an answer_check.v1 reply with these verdicts for statements 1, 2 and so on."""
        out = [{"id": str(n), "verdict": v, "reason": "r"} for n, v in enumerate(given, 1)]
        return httpx.Response(
            200,
            json={"output": json.dumps({"verdicts": out}), "model_version": "gpt-check", "usage_tokens": 7},
        )

    def calls(self, path: str) -> list[dict[str, Any]]:
        return [body for p, body, _ in self.requests if p == path]


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def api(fakes: Fakes, store: MemoryStore) -> Iterator[TestClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fakes.handle))
    main.app.state.settings, main.app.state.store = SETTINGS, store
    main.app.state.services, main.app.state.pending = Services(SETTINGS, http), set()
    with as_caller(main.app, testing.user_actor):
        yield TestClient(main.app)


def as_user(user: str = "s1") -> dict[str, str]:
    return testing.as_user(user)
