"""A chat turn end to end against the fakes: rewrite, search with the user's token, streamed and cited answer, and
what is saved. Ported from fsd_test's assistant tests."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

import anyio
import httpx
import pytest
from conftest import OTHER_ROOM, ROOM, SETTINGS, Fakes, MemoryStore, as_user, chat_stream, rewrite
from fastapi.testclient import TestClient
from learning_transport import sse_event

import main
import prompts
from chat import needs_rewrite
from clients import Services

C1, C2, C3 = str(uuid4()), str(uuid4()), str(uuid4())
DOC = str(uuid4())


def result(chunk_id: str, content: str, page: int, **extra: Any) -> dict[str, Any]:
    """A Content /v1/search result, with every field it returns."""
    return {"chunk_id": chunk_id, "document_id": DOC, "title": "Biology: Photosynthesis",
            "doc_type": "teaching_material", "section_path": "Light reactions", "page_start": page, "page_end": page,
            "content": content, "kind": "text", "source": "pdf", "image": None, "score": 0.9, "related": [],
            **extra}  # fmt: skip


RESULTS = [
    result(C1, "Chlorophyll absorbs light.", 3),
    result(C2, "Stomata let CO2 in.", 5),
    result(C3, "Figure: the leaf.", 6, kind="figure", source="vision", image="0123456789abcdef.png"),
]
LOW = [
    result(C1, "Chlorophyll absorbs light.", 3, score=0.60),
    result(C2, "Stomata let CO2 in.", 5, score=0.2),
]


def thread(api: TestClient, user: str = "s1", classroom: str = ROOM) -> str:
    resp = api.post("/v1/threads", json={"classroom_id": classroom}, headers=as_user(user))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def post(api: TestClient, tid: str, content: str, user: str = "s1") -> list[tuple[str, dict[str, Any]]]:
    """POST a message and parse the SSE stream into (event, data) pairs."""
    resp = api.post(f"/v1/threads/{tid}/messages", json={"content": content}, headers=as_user(user))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = []
    for frame in resp.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def tokens(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(d["text"] for e, d in events if e == "token")


def saved(store: MemoryStore, tid: str) -> dict[str, Any]:
    return store.all(tid)[-1]


def test_answer_streams_cites_and_saves(api: TestClient, fakes: Fakes, store: MemoryStore) -> None:
    fakes.results = RESULTS
    fakes.replies += [rewrite("How do plants get energy from light?"),
                      chat_stream(["Light is absorbed [S", "1]. Stomata", " [S2, S9] open."])]  # fmt: skip
    tid = thread(api)
    events = post(api, tid, "how do plants get energy?")

    assert [e for e, _ in events][-2:] == ["citations", "done"]
    assert tokens(events) == f"Light is absorbed [doc:{C1}]. Stomata [doc:{C2}] open."
    citations = events[-2][1]["citations"]
    assert [c["chunk_id"] for c in citations] == [C1, C2]
    assert set(citations[0]) == {"chunk_id", "document_id", "title", "section_path", "page_start", "page_end", "kind", "source",
                                 "content", "image"}  # fmt: skip
    answer = saved(store, tid)
    assert events[-1][1] == {"message_id": answer["id"], "outcome": "answered"}
    assert (answer["outcome"], answer["prompt_version"], answer["model"], answer["usage_tokens"]) == (
        "answered", "student_tutor.v3", "gpt-test", 42)  # fmt: skip
    assert answer["rewritten_query"] == "How do plants get energy from light?"
    assert answer["retrieved_chunk_ids"] == [C1, C2, C3]

    # Content got the rewritten query and the student's own token; ai-core got the agent's (checked by the fake).
    [search] = fakes.calls("/v1/search")
    assert search == {"classroom_id": ROOM, "query": "How do plants get energy from light?", "top_k": 8,
                      "rerank": True}  # fmt: skip
    request = next(r for p, _, r in fakes.requests if p == "/v1/search")
    assert request.headers["authorization"] == "Bearer s1"
    rewrite_call, answer_call = fakes.calls("/v1/inference")
    assert (
        rewrite_call["response_schema"] == prompts.REWRITE_SCHEMA
        and rewrite_call["schema_name"] == "query_rewrite.v3"
    )
    assert answer_call["stream"] is True
    assert "Chlorophyll absorbs light." in answer_call["messages"][2]["content"]


def test_messages_list_and_title(api: TestClient, fakes: Fakes) -> None:
    fakes.results = RESULTS
    fakes.replies += [rewrite("q"), chat_stream(["One [S1]."]), rewrite("q2"), chat_stream(["Two."])]
    tid = thread(api)
    post(api, tid, "first question")
    post(api, tid, "and the second?")
    second_answer = fakes.calls("/v1/inference")[3]["messages"]
    assert [m["role"] for m in second_answer[3:]] == ["user", "assistant", "user"]
    assert second_answer[4]["content"] == "One."  # earlier answers lose their [doc:…] markers
    listed = api.get(f"/v1/threads/{tid}/messages", headers=as_user()).json()["messages"]
    assert [(m["role"], m["outcome"]) for m in listed] == [("user", None), ("assistant", "answered")] * 2
    assert listed[1]["content"] == f"One [doc:{C1}]."
    assert listed[1]["citations"][0]["chunk_id"] == C1
    [listed_thread] = api.get("/v1/threads", headers=as_user()).json()["threads"]
    assert listed_thread["title"] == "first question"


@pytest.mark.parametrize(
    ("user", "reply"), [("s1", prompts.REFUSALS["student"]), ("t1", prompts.REFUSALS["teacher"])]
)
def test_top_score_below_the_floor_is_refused_without_a_model_call(
    api: TestClient, fakes: Fakes, store: MemoryStore, user: str, reply: str
) -> None:
    fakes.results = LOW
    fakes.replies.append(rewrite("q"))
    tid = thread(api, user)
    events = post(api, tid, "q", user)
    assert tokens(events) == reply and events[-2][1] == {"citations": []}
    assert saved(store, tid)["outcome"] == "no_results"
    assert len(fakes.calls("/v1/inference")) == 1  # the rewrite only


def test_rerank_fallback_fails_the_turn(api: TestClient, fakes: Fakes, store: MemoryStore) -> None:
    """RRF scores can't be checked against the floor, so a fallback search is not answered."""
    fakes.results, fakes.fallback = [result(C1, "Chlorophyll absorbs light.", 3, score=0.03)], True
    fakes.replies.append(rewrite("q"))
    tid = thread(api)
    events = post(api, tid, "q")
    assert events == [("error", {"code": "SEARCH_UNAVAILABLE", "message": events[0][1]["message"]})]
    assert saved(store, tid)["outcome"] == "error"
    assert len(fakes.calls("/v1/inference")) == 1  # the rewrite only: no answer was generated


@pytest.mark.parametrize("results", [[], LOW], ids=["no-results", "low"])
def test_rewrite_failure_searches_the_raw_message(api: TestClient, fakes: Fakes, results: list[Any]) -> None:
    fakes.results = results
    fakes.replies.append(httpx.Response(502, json={"error": {"code": "AI_OUTPUT_REJECTED", "message": "x"}}))
    events = post(api, thread(api), "raw question")
    assert fakes.calls("/v1/search")[0]["query"] == "raw question"
    assert events[-1][1]["outcome"] == "no_results"


def test_off_topic_is_refused_without_a_search(api: TestClient, fakes: Fakes) -> None:
    fakes.replies.append(rewrite("tell me a joke", on_topic=False))
    assert post(api, thread(api), "tell me a joke")[-1][1]["outcome"] == "no_results"
    assert fakes.calls("/v1/search") == []


def test_small_talk_is_answered_without_documents(api: TestClient, fakes: Fakes, store: MemoryStore) -> None:
    fakes.replies += [rewrite("thanks", small_talk=True), chat_stream(["You're welcome [S1]!"])]
    tid = thread(api)
    events = post(api, tid, "thanks!")
    assert tokens(events) == "You're welcome !" and fakes.calls("/v1/search") == []
    assert saved(store, tid)["prompt_version"] == "small_talk.v1"
    assert not any("documents" in m["content"] for m in fakes.calls("/v1/inference")[1]["messages"])


@pytest.mark.parametrize(
    ("status", "code"),
    [(503, "SEARCH_UNAVAILABLE"), (403, "FORBIDDEN")],
)
def test_search_failures_are_error_events_and_saved(
    api: TestClient, fakes: Fakes, store: MemoryStore, status: int, code: str
) -> None:
    fakes.search_status = status
    fakes.replies.append(rewrite("q"))
    tid = thread(api)
    events = post(api, tid, "q")
    assert events == [("error", {"code": code, "message": events[0][1]["message"]})]
    assert saved(store, tid)["outcome"] == "error"


def test_ai_core_unavailable_fails_the_turn(api: TestClient, fakes: Fakes, store: MemoryStore) -> None:
    fakes.replies.append(
        httpx.Response(503, json={"error": {"code": "AI_PROVIDER_NOT_CONFIGURED", "message": "x"}})
    )
    tid = thread(api)
    assert post(api, tid, "q")[0] == ("error", {"code": "AI_UNAVAILABLE", "message": "The assistant is not "
                                               "available right now."})  # fmt: skip
    assert fakes.calls("/v1/search") == [] and saved(store, tid)["outcome"] == "error"


def test_a_failure_mid_stream_saves_the_partial_answer(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    fakes.results = RESULTS
    fakes.replies += [rewrite("q"), chat_stream(["Light is [S1] "], fail=True)]
    tid = thread(api)
    events = post(api, tid, "q")
    assert [e for e, _ in events] == ["token", "error"] and events[1][1]["code"] == "AI_PROVIDER_FAILED"
    answer = saved(store, tid)
    assert (answer["content"], answer["outcome"]) == (f"Light is [doc:{C1}] ", "error")
    assert [c["chunk_id"] for c in answer["citations"]] == [C1]


def test_disconnect_saves_an_interrupted_answer(api: TestClient, fakes: Fakes, store: MemoryStore) -> None:
    async def hang() -> AsyncIterator[bytes]:
        yield sse_event("delta", {"text": "Partial [S1] "}).encode()
        await asyncio.Event().wait()

    fakes.results = RESULTS
    fakes.replies += [rewrite("q"), httpx.Response(200, content=hang())]  # type: ignore[list-item]
    tid = thread(api)
    anyio.run(post_then_disconnect, api.app, tid)
    answer = saved(store, tid)
    assert (answer["content"], answer["outcome"]) == (f"Partial [doc:{C1}] ", "interrupted")


def test_a_client_gone_before_the_stream_starts_still_gets_an_answer_saved() -> None:
    """Starlette never starts the events generator when sending the response start fails, so the response's
    finish saves the turn instead: the user's message never stands alone."""
    from starlette.requests import ClientDisconnect

    from chat import SSEResponse, Turn, TurnRequest

    async def scenario() -> list[dict[str, Any]]:
        store, pending = MemoryStore(), set[asyncio.Task[Any]]()
        asked = TurnRequest(thread={"id": "t", "role": "student", "classroom_id": ROOM}, authorization="Bearer s1",
                            correlation="c", content="q", history=[], billed_to={})  # fmt: skip
        turn = Turn(None, store, SETTINGS, pending, asked)  # type: ignore[arg-type]
        response = SSEResponse(turn.events(), turn.finish)

        async def receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            raise OSError("the client is gone")

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)  # type: ignore[arg-type]
        await asyncio.wait(set(pending), timeout=5)
        return store.all("t")

    [row] = anyio.run(scenario)
    assert (row["content"], row["outcome"]) == ("", "interrupted")


async def post_then_disconnect(app: Any, tid: str) -> None:
    """Drive the ASGI app directly: the client disconnects once the first token has been sent."""
    first_token = asyncio.Event()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": json.dumps({"content": "q"}).encode(), "more_body": False}
        await first_token.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and b"event: token" in message.get("body", b""):
            first_token.set()

    path = f"/v1/threads/{tid}/messages"
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"content-type", b"application/json"), (b"authorization", b"Bearer s1")],
             "client": ("127.0.0.1", 1), "server": ("testserver", 80)}  # fmt: skip
    await asyncio.wait_for(app(scope, receive, send), 10)
    assert first_token.is_set()
    if app.state.pending:
        await asyncio.wait(set(app.state.pending), timeout=10)


# ---- threads and access


def test_threads_need_classroom_access(api: TestClient, fakes: Fakes) -> None:
    assert (
        api.post("/v1/threads", json={"classroom_id": OTHER_ROOM}, headers=as_user("s1")).status_code == 403
    )
    resp = api.post("/v1/threads", json={"classroom_id": ROOM}, headers=as_user("t1"))
    assert resp.status_code == 201 and resp.json()["role"] == "teacher"
    access = next(r for p, _, r in fakes.requests if p.endswith("/access"))
    assert access.headers["authorization"] == "Bearer s1"  # Identity decides, with the user's own token


def test_a_removed_student_loses_their_threads(api: TestClient, fakes: Fakes, store: MemoryStore) -> None:
    tid = thread(api, "s1")
    fakes.access.discard(("Bearer s1", ROOM))
    assert api.get(f"/v1/threads/{tid}/messages", headers=as_user("s1")).status_code == 403
    resp = api.post(f"/v1/threads/{tid}/messages", json={"content": "q"}, headers=as_user("s1"))
    assert resp.status_code == 403 and store.all(tid) == []
    assert api.get("/v1/threads", params={"classroom_id": ROOM}, headers=as_user("s1")).status_code == 403


def test_a_removed_student_no_longer_lists_those_threads(api: TestClient, fakes: Fakes) -> None:
    """The unfiltered list is checked too: a thread title is a question asked about that classroom's materials."""
    fakes.access.add(("Bearer s1", OTHER_ROOM))
    kept = thread(api, "s1", classroom=OTHER_ROOM)
    thread(api, "s1", classroom=ROOM)
    assert len(api.get("/v1/threads", headers=as_user("s1")).json()["threads"]) == 2
    fakes.access.discard(("Bearer s1", ROOM))
    resp = api.get("/v1/threads", headers=as_user("s1"))
    assert resp.status_code == 200 and [t["id"] for t in resp.json()["threads"]] == [kept]


def answering(handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """The other services answer through `handler` from now on."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    main.app.state.services = Services(SETTINGS, http)


def test_the_thread_list_needs_identity(api: TestClient, fakes: Fakes) -> None:
    thread(api, "s1")
    answering(lambda r: httpx.Response(503) if r.url.path.endswith("/access") else fakes.handle(r))
    resp = api.get("/v1/threads", headers=as_user("s1"))
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "DEPENDENCY_UNAVAILABLE")


def test_only_students_and_teachers(api: TestClient) -> None:
    assert api.post("/v1/threads", json={"classroom_id": ROOM}, headers=as_user("x1")).status_code == 403
    assert api.get("/v1/threads", headers=as_user("x1")).status_code == 403


@pytest.mark.parametrize("user", ["s2", "t1"])
def test_someone_elses_thread_is_404_and_nothing_saved(
    api: TestClient, store: MemoryStore, user: str
) -> None:
    tid = thread(api, "s1")
    resp = api.post(f"/v1/threads/{tid}/messages", json={"content": "q"}, headers=as_user(user))
    assert resp.status_code == 404
    assert api.get(f"/v1/threads/{tid}/messages", headers=as_user(user)).status_code == 404
    assert store.all(tid) == [] and api.get("/v1/threads", headers=as_user(user)).json()["threads"] == []


@pytest.mark.parametrize("content", ["", "   ", "x" * 1001])
def test_invalid_content_is_422_and_nothing_saved(api: TestClient, store: MemoryStore, content: str) -> None:
    tid = thread(api)
    resp = api.post(f"/v1/threads/{tid}/messages", json={"content": content}, headers=as_user())
    assert resp.status_code == 422 and store.all(tid) == []


def test_threads_filter_by_classroom(api: TestClient, fakes: Fakes) -> None:
    fakes.access.add(("Bearer s1", OTHER_ROOM))
    thread(api, classroom=ROOM)
    thread(api, classroom=OTHER_ROOM)
    listed = api.get("/v1/threads", params={"classroom_id": OTHER_ROOM}, headers=as_user()).json()["threads"]
    assert [t["classroom_id"] for t in listed] == [OTHER_ROOM]


def test_no_anonymous_access() -> None:
    from main import app

    client = TestClient(app)
    assert client.post("/v1/threads", json={"classroom_id": ROOM}).status_code == 401
    assert client.get("/v1/threads").status_code == 401


LIMIT = {"error": {"code": "AI_SPEND_LIMIT", "message": "x", "resets_at": "2026-10-14T00:00:00+08:00"}}


def test_model_calls_are_billed_to_the_user(api: TestClient, fakes: Fakes) -> None:
    fakes.results = RESULTS
    fakes.replies += [rewrite("q"), chat_stream(["One [S1]."])]
    post(api, thread(api, "t1"), "q", user="t1")
    billed = [call["billed_to"] for call in fakes.calls("/v1/inference")]
    assert billed == [{"user_id": "t1", "org_id": billed[0]["org_id"], "role": "teacher"}] * 2


def test_a_used_up_allowance_ends_the_turn_before_searching(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    fakes.replies.append(httpx.Response(429, json=LIMIT))
    tid = thread(api)
    assert post(api, tid, "q") == [
        ("error", {"code": "AI_SPEND_LIMIT", "message": "You've used today's AI allowance.",
                   "resets_at": "2026-10-14T00:00:00+08:00"})
    ]  # fmt: skip
    assert fakes.calls("/v1/search") == [] and saved(store, tid)["outcome"] == "error"


def test_a_used_up_allowance_during_search(api: TestClient, fakes: Fakes) -> None:
    fakes.replies.append(rewrite("q"))
    fakes.search_status = 429
    fakes.search_error = LIMIT
    events = post(api, thread(api), "q")
    assert events[-1][0] == "error" and events[-1][1]["code"] == "AI_SPEND_LIMIT"


BUSY = {"error": {"code": "AI_BUSY", "message": "x"}}


def test_too_many_running_calls_end_the_turn_before_searching(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    fakes.replies.append(httpx.Response(429, json=BUSY))
    tid = thread(api)
    [(event, data)] = post(api, tid, "q")
    assert event == "error" and data["code"] == "AI_BUSY" and "resets_at" not in data
    assert fakes.calls("/v1/search") == [] and saved(store, tid)["outcome"] == "error"


def test_too_many_running_calls_during_search(api: TestClient, fakes: Fakes) -> None:
    fakes.replies.append(rewrite("q"))
    fakes.search_status = 429
    fakes.search_error = BUSY
    events = post(api, thread(api), "q")
    assert events[-1][0] == "error" and events[-1][1]["code"] == "AI_BUSY"


def test_usage(api: TestClient, fakes: Fakes) -> None:
    resp = api.get("/v1/usage", headers=as_user("s1"))
    assert resp.status_code == 200 and resp.json()["limit_usd"] == 5.0
    [(_, _, request)] = [r for r in fakes.requests if r[0] == "/v1/usage"]
    assert request.url.params["user_id"] == "s1" and request.url.params["role"] == "student"


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(503),
        httpx.Response(500),
        httpx.ConnectError("down"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"spent_usd": 1.0}),
        httpx.Response(
            200, json={"spent_usd": "lots", "limit_usd": 5, "resets_at": "2026-10-19T00:00:00+08:00"}
        ),
    ],
    ids=["503", "500", "unreachable", "not-json", "not-an-object", "missing-fields", "wrong-type"],
)
def test_usage_is_503_when_ai_core_cannot_answer(
    api: TestClient, fakes: Fakes, failure: httpx.Response | Exception
) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/usage":
            return fakes.handle(request)
        if isinstance(failure, Exception):
            raise failure
        return failure

    answering(broken)
    resp = api.get("/v1/usage", headers=as_user("s1"))
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "DEPENDENCY_UNAVAILABLE")


@pytest.mark.parametrize(("user", "role"), [("s1", "student"), ("t1", "teacher")])
def test_the_usage_meter_and_a_turn_use_the_same_allowance(
    api: TestClient, fakes: Fakes, user: str, role: str
) -> None:
    """An account holds one role, so the meter shows the allowance its chat turns are billed to."""
    fakes.results = RESULTS
    fakes.replies += [rewrite("q"), chat_stream(["One [S1]."])]
    post(api, thread(api, user), "q", user=user)
    assert [call["billed_to"]["role"] for call in fakes.calls("/v1/inference")] == [role, role]
    assert api.get("/v1/usage", headers=as_user(user)).status_code == 200
    [request] = [r for p, _, r in fakes.requests if p == "/v1/usage"]
    assert request.url.params["role"] == role


def test_no_thread_in_a_purged_classroom(api: TestClient, store: MemoryStore) -> None:
    store.purged.add(ROOM)
    resp = api.post("/v1/threads", json={"classroom_id": ROOM}, headers=as_user("s1"))
    assert resp.status_code == 403 and store.threads == {}


def test_messages_are_the_latest_oldest_first(api: TestClient, store: MemoryStore) -> None:
    tid = thread(api)
    for n in range(3):
        anyio.run(store.start_turn, tid, f"q{n}", 6)
    resp = api.get(f"/v1/threads/{tid}/messages", params={"limit": 2}, headers=as_user())
    assert [m["content"] for m in resp.json()["messages"]] == ["q1", "q2"]
    assert api.get(f"/v1/threads/{tid}/messages", params={"limit": 201}, headers=as_user()).status_code == 422


def test_a_disconnect_during_the_final_save_keeps_the_answer(fakes: Fakes) -> None:
    from conftest import SETTINGS

    from chat import Turn, TurnRequest

    class SlowStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.saving, self.release = asyncio.Event(), asyncio.Event()

        async def save_answer(self, thread_id: str, answer: dict[str, Any]) -> str:
            self.saving.set()
            await self.release.wait()
            return await super().save_answer(thread_id, answer)

    async def answer() -> AsyncIterator[str]:
        yield sse_event("token", {"text": "Done."})

    async def scenario() -> list[dict[str, Any]]:
        store, pending = SlowStore(), set[asyncio.Task[Any]]()
        asked = TurnRequest(thread={"id": "t", "role": "student", "classroom_id": ROOM}, authorization="Bearer s1",
                            correlation="c", content="q", history=[], billed_to={})  # fmt: skip
        turn = Turn(None, store, SETTINGS, pending, asked)  # type: ignore[arg-type]
        turn.parts.append("Done.")
        turn._answer = answer  # type: ignore[method-assign]

        async def drain() -> None:
            async for _ in turn.events():
                pass

        task = asyncio.create_task(drain())
        await store.saving.wait()
        task.cancel()  # the client goes away while the answer is being saved
        with pytest.raises(asyncio.CancelledError):
            await task
        store.release.set()
        await asyncio.wait(set(pending), timeout=5)
        return store.all("t")

    [row] = anyio.run(scenario)
    assert (row["content"], row["outcome"]) == ("Done.", "answered")


def test_results_that_do_not_fit_are_dropped_and_cannot_be_cited(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    """Eight long passages: only the first five fit in one prompt message. A tag for a trimmed source ([S8]) is
    dropped from the answer and is not among the citations or the retrieved chunks."""
    from chat import MAX_DOCUMENTS_CHARS, PASSAGE_OVERHEAD

    size = MAX_DOCUMENTS_CHARS // 5 - PASSAGE_OVERHEAD - 20  # with "passage n ": five fit, six do not
    ids = [str(uuid4()) for _ in range(8)]
    fakes.results = [result(c, f"passage {n} " + "x" * size, n) for n, c in enumerate(ids, 1)]
    fakes.replies += [rewrite("q"), chat_stream(["Kept [S1]. Trimmed [S8]. Both [S5, S6]."])]
    tid = thread(api)
    events = post(api, tid, "q")

    assert tokens(events) == f"Kept [doc:{ids[0]}]. Trimmed . Both [doc:{ids[4]}]."
    assert [c["chunk_id"] for c in events[-2][1]["citations"]] == [ids[0], ids[4]]
    answer = saved(store, tid)
    assert answer["retrieved_chunk_ids"] == ids[:5]
    prompt = fakes.calls("/v1/inference")[1]["messages"][2]["content"]
    assert 'id="S5"' in prompt and 'id="S6"' not in prompt and len(prompt) <= MAX_DOCUMENTS_CHARS + 1000


def purge_first(store: MemoryStore, step: str, tid: str) -> None:
    """The thread is purged (its classroom or user deleted) just before the store's `step` runs."""
    original = getattr(store, step)

    async def purged(*args: Any) -> Any:
        store.gone.add(tid)
        return await original(*args)

    setattr(store, step, purged)


def test_a_thread_purged_before_the_turn_starts_is_not_found(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    tid = thread(api)
    purge_first(store, "start_turn", tid)
    resp = api.post(f"/v1/threads/{tid}/messages", json={"content": "q"}, headers=as_user())
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "NOT_FOUND")
    assert store.all(tid) == [] and not fakes.calls("/v1/inference")


def test_a_thread_purged_while_answering_ends_with_an_error_event(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    fakes.results = RESULTS
    fakes.replies += [rewrite("light"), chat_stream(["Light is absorbed [S1]."])]
    tid = thread(api)
    purge_first(store, "save_answer", tid)
    events = post(api, tid, "how do plants get energy?")
    assert events[-1] == ("error", {"code": "NOT_FOUND", "message": "This conversation was deleted."})
    assert "done" not in [e for e, _ in events]


def test_a_failing_turn_whose_thread_is_purged_says_so(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    fakes.replies += [rewrite("light")]
    fakes.search_status = 503
    tid = thread(api)
    purge_first(store, "save_answer", tid)
    assert post(api, tid, "q")[-1][1]["code"] == "NOT_FOUND"


def test_an_answer_that_cannot_be_saved_ends_with_an_error_event(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    fakes.results = RESULTS
    fakes.replies += [rewrite("light"), chat_stream(["Light is absorbed [S1]."])]
    tid = thread(api)

    async def broken(*_: Any) -> str:
        raise ConnectionError("database down")

    store.save_answer = broken  # type: ignore[method-assign]
    events = post(api, tid, "how do plants get energy?")
    assert events[-1] == ("error", {"code": "INTERNAL_ERROR", "message": "The answer could not be saved."})


def topic(n: int, document: str, score: float, title: str) -> list[dict[str, Any]]:
    """n results of one document, best first, starting at `score`."""
    return [result(str(uuid4()), f"{title} passage {i}", i, document_id=document, title=title,
                   score=round(score - i / 100, 2)) for i in range(1, n + 1)]  # fmt: skip


def test_a_message_about_several_topics_searches_each_and_shares_the_prompt(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    """The whole message and every topic are searched, and the searches take turns: the testing topic, which the
    whole message's search never reaches, still gets passages, and no document has more than PER_DOCUMENT
    (so two documents give ten passages, not MERGED_TOP_K)."""
    from chat import MERGED_TOP_K, PER_DOCUMENT

    design, testing = str(uuid4()), str(uuid4())
    whole = topic(8, design, 0.95, "Software Design")
    fakes.by_query = {
        "how does design relate to testing": whole,
        "design principles": whole[:4] + topic(4, design, 0.9, "Software Design"),
        "unit testing with mocks": topic(8, testing, 0.85, "Software Testing"),
    }
    fakes.replies += [
        rewrite(
            "how does design relate to testing", sub_queries=["design principles", "unit testing with mocks"]
        ),
        chat_stream(["Design [S1] makes tests easy [S3]."]),
    ]
    tid = thread(api)
    events = post(api, tid, "how does design make testing easier?")

    assert [c["query"] for c in fakes.calls("/v1/search")] == [
        "how does design relate to testing", "design principles", "unit testing with mocks"]  # fmt: skip
    answer = saved(store, tid)
    assert answer["outcome"] == "answered"
    assert answer["sub_queries"] == ["design principles", "unit testing with mocks"]
    by_id = {r["chunk_id"]: r for rs in fakes.by_query.values() for r in rs}
    docs = [by_id[c]["document_id"] for c in answer["retrieved_chunk_ids"]]
    assert len(docs) == len(set(answer["retrieved_chunk_ids"])) == 2 * PER_DOCUMENT <= MERGED_TOP_K
    assert docs.count(design) == docs.count(testing) == PER_DOCUMENT
    # Turns: the whole message's best, then design's next new one, then testing's best, and so on.
    assert docs[:3] == [design, design, testing]
    assert [c["title"] for c in events[-2][1]["citations"]] == ["Software Design", "Software Testing"]
    prompt = fakes.calls("/v1/inference")[1]["messages"]
    documents = prompt[2]["content"]
    assert "Software Testing passage 1" in documents and "explain the connection" in prompt[0]["content"]
    # The topics are listed, and each passage a topic's search found names it; the whole message's don't.
    assert (
        "<topics><topic>design principles</topic><topic>unit testing with mocks</topic></topics>" in documents
    )
    assert 'pages="1" topic="unit testing with mocks">\nSoftware Testing passage 1' in documents
    assert 'pages="1" topic="design principles">\nSoftware Design passage 1' in documents


def test_a_topic_whose_best_passage_is_under_the_floor_gives_none(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    good, weak = str(uuid4()), str(uuid4())
    fakes.by_query = {
        "git and kanban": topic(3, good, 0.9, "Git"),
        "git branches": topic(3, good, 0.9, "Git"),
        "kanban boards": topic(3, weak, 0.4, "Unrelated"),
    }
    fakes.replies += [rewrite("git and kanban", sub_queries=["git branches", "kanban boards"]),
                      chat_stream(["Branches [S1]."])]  # fmt: skip
    tid = thread(api)
    post(api, tid, "how do git and kanban fit together?")
    retrieved = saved(store, tid)["retrieved_chunk_ids"]
    assert retrieved and not {r["chunk_id"] for r in fakes.by_query["kanban boards"]} & set(retrieved)


def test_every_search_under_the_floor_is_refused(api: TestClient, fakes: Fakes, store: MemoryStore) -> None:
    fakes.results = LOW
    fakes.replies.append(rewrite("a and b", sub_queries=["a", "b"]))
    tid = thread(api)
    post(api, tid, "a and b?")
    assert saved(store, tid)["outcome"] == "no_results" and len(fakes.calls("/v1/search")) == 3
    assert len(fakes.calls("/v1/inference")) == 1


def test_one_topic_is_one_search(api: TestClient, fakes: Fakes, store: MemoryStore) -> None:
    fakes.results = RESULTS
    fakes.replies += [rewrite("photosynthesis", sub_queries=["photosynthesis", " Photosynthesis "]),
                      chat_stream(["One [S1]."])]  # fmt: skip
    tid = thread(api)
    post(api, tid, "photosynthesis?")
    assert len(fakes.calls("/v1/search")) == 1 and saved(store, tid)["sub_queries"] is None


def test_a_topic_search_without_the_reranker_fails_the_turn(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    fakes.results, fakes.fallback_queries = RESULTS, {"b"}
    fakes.replies.append(rewrite("a and b", sub_queries=["a", "b"]))
    tid = thread(api)
    assert post(api, tid, "a and b?")[-1][1]["code"] == "SEARCH_UNAVAILABLE"
    assert saved(store, tid)["outcome"] == "error" and len(fakes.calls("/v1/inference")) == 1


def test_topics_are_cleaned() -> None:
    from chat import topics

    assert topics(["a", " A ", "", "q", "b", "c", "d"], "q") == ["a", "b", "c"]
    assert topics(["only one"], "q") == [] and topics("not a list", "q") == [] and topics(None, "q") == []


def test_merge_stops_at_merged_top_k() -> None:
    from chat import MERGED_TOP_K, merge

    searches = [topic(8, str(uuid4()), 0.9, f"Doc {n}") for n in range(3)]
    merged = merge(searches, 0.61, 0.5)
    assert len(merged) == MERGED_TOP_K
    assert [(r["title"], n) for r, n in merged[:4]] == [
        ("Doc 0", 0),
        ("Doc 1", 1),
        ("Doc 2", 2),
        ("Doc 0", 0),
    ]


def test_a_topic_search_has_the_lower_topic_floor() -> None:
    """A topic's search at 0.55 adds passages; the whole message's search at 0.55 doesn't."""
    from chat import merge

    whole, narrow = topic(2, str(uuid4()), 0.56, "Whole"), topic(2, str(uuid4()), 0.56, "Narrow")
    strong = topic(2, str(uuid4()), 0.9, "Strong")
    assert {r["title"] for r, _ in merge([whole, strong, narrow], 0.61, 0.5)} == {"Strong", "Narrow"}


def test_the_turn_log_says_where_the_time_went(
    api: TestClient, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    fakes.results = RESULTS
    fakes.replies += [rewrite("q"), chat_stream(["One [S1]."])]
    with caplog.at_level("INFO", logger="question-chatbot-agent.chat"):
        post(api, thread(api), "q")
    [turn] = [r for r in caplog.records if r.getMessage() == "chat_turn"]
    steps = ("rewrite_ms", "search_ms", "first_token_ms", "answer_ms")
    assert all(isinstance(getattr(turn, s), int) for s in steps)


def test_a_second_question_while_one_is_answering_is_refused(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    fakes.results = RESULTS
    first, second = thread(api), thread(api)
    asyncio.run(store.claim_turn(first))  # the first tab's answer is still streaming
    resp = api.post(f"/v1/threads/{second}/messages", json={"content": "and stomata?"}, headers=as_user())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TURN_IN_PROGRESS"
    assert store.all(second) == [] and fakes.calls("/v1/search") == []
    # Someone else's answer doesn't hold this user up.
    fakes.replies += [rewrite("stomata"), chat_stream(["Stomata let CO2 in [S2]."])]
    assert post(api, thread(api, "t1"), "and stomata?", "t1")[-1][0] == "done"


def test_the_next_question_is_accepted_once_the_answer_is_saved(api: TestClient, fakes: Fakes) -> None:
    fakes.results = RESULTS
    tid = thread(api)
    for question in ("how do plants get energy?", "and then?"):
        fakes.replies += [rewrite(question), chat_stream(["Light is absorbed [S1]."])]
        assert post(api, tid, question)[-1][0] == "done"
    fakes.replies += [rewrite("stomata"), chat_stream(["Stomata let CO2 in [S2]."], fail=True)]
    assert post(api, tid, "stomata?")[-1][0] == "error"  # a failed answer is saved too, and ends the claim
    fakes.replies += [rewrite("stomata"), chat_stream(["Stomata let CO2 in [S2]."])]
    assert post(api, tid, "stomata?")[-1][0] == "done"


def test_a_standalone_definition_skips_rewrite_but_still_requires_grounding(
    api: TestClient, fakes: Fakes
) -> None:
    main.app.state.settings = replace(SETTINGS, adaptive_rewrite=True)
    fakes.results = RESULTS
    fakes.replies.append(chat_stream(["Chlorophyll absorbs light [S1]."]))
    events = post(api, thread(api), "What is chlorophyll?")
    assert events[-1][0] == "done"
    calls = fakes.calls("/v1/inference")
    assert len(calls) == 1 and calls[0]["stream"] is True
    assert fakes.calls("/v1/search")[0]["query"] == "What is chlorophyll?"
    assert needs_rewrite("Explain that further", []) is True
    assert needs_rewrite("What is chlorophyll?", [{"role": "user", "content": "Earlier context"}]) is True
    assert needs_rewrite("Compare X and Y", []) is True


def test_covered_topics_reuse_the_primary_search(api: TestClient, fakes: Fakes) -> None:
    main.app.state.settings = replace(SETTINGS, adaptive_search=True)
    fakes.results = [
        result(
            C1, "Chlorophyll and stomata are parts of the leaf.", 1, section_path="Chlorophyll and stomata"
        )
    ]
    fakes.replies += [
        rewrite("chlorophyll and stomata", sub_queries=["chlorophyll", "stomata"]),
        chat_stream(["The leaf [S1]."]),
    ]
    assert post(api, thread(api), "How do chlorophyll and stomata work together?")[-1][0] == "done"
    assert len(fakes.calls("/v1/search")) == 1


def test_context_budget_refuses_instead_of_sending_an_oversized_first_passage(
    api: TestClient, fakes: Fakes
) -> None:
    main.app.state.settings = replace(SETTINGS, context_tokens=2000)
    fakes.results = [result(C1, "chlorophyll " * 10000, 1)]
    fakes.replies.append(rewrite("chlorophyll"))
    events = post(api, thread(api), "chlorophyll?")
    assert events[-1][1]["outcome"] == "no_results"
    assert len(fakes.calls("/v1/inference")) == 1


def test_body_mentions_do_not_skip_topic_searches(api: TestClient, fakes: Fakes) -> None:
    main.app.state.settings = replace(SETTINGS, adaptive_search=True)
    fakes.results = [result(C1, "Chlorophyll and stomata are mentioned in this overview.", 1)]
    fakes.replies += [
        rewrite("chlorophyll and stomata", sub_queries=["chlorophyll", "stomata"]),
        chat_stream(["The leaf [S1]."]),
    ]
    post(api, thread(api), "How do chlorophyll and stomata work together?")
    assert len(fakes.calls("/v1/search")) == 3


def test_duplicate_primary_passage_keeps_all_topic_labels(api: TestClient, fakes: Fakes) -> None:
    main.app.state.settings = replace(SETTINGS, adaptive_search=True)
    fakes.results = [result(C1, "Leaf processes.", 1, section_path="Chlorophyll and stomata")]
    fakes.replies += [
        rewrite("leaf processes", sub_queries=["chlorophyll", "stomata"]),
        chat_stream(["The leaf [S1]."]),
    ]
    events = post(api, thread(api), "How do chlorophyll and stomata work together?")
    docs = fakes.calls("/v1/inference")[1]["messages"][2]["content"]
    assert 'topic="chlorophyll; stomata"' in docs
    assert "Citation coverage is incomplete" not in tokens(events)


def test_missing_topic_citation_is_reported_without_another_model_call(api: TestClient, fakes: Fakes) -> None:
    one, two = str(uuid4()), str(uuid4())
    fakes.by_query = {
        "both": [],
        "chlorophyll": [result(C1, "Light.", 1, document_id=one)],
        "stomata": [result(C2, "Gas exchange.", 2, document_id=two)],
    }
    fakes.replies += [rewrite("both", sub_queries=["chlorophyll", "stomata"]), chat_stream(["Light [S1]."])]
    reply = tokens(post(api, thread(api), "How do both work?"))
    assert "Citation coverage is incomplete for: stomata." in reply
    assert len(fakes.calls("/v1/inference")) == 2


def test_tight_context_reserves_room_for_each_topic(
    api: TestClient, fakes: Fakes, store: MemoryStore
) -> None:
    from chat import PASSAGE_OVERHEAD, token_bound

    question = "How do chlorophyll and stomata work together?"
    passages = [result(cid, "Leaf biology. " * 100, n) for n, cid in enumerate([C1, C2, C3], 1)]
    cost = token_bound(passages[0]["content"]) + token_bound(passages[0]["title"]) + PASSAGE_OVERHEAD
    main.app.state.settings = replace(SETTINGS, context_tokens=1000 + token_bound(question) + 2 * cost)
    fakes.by_query = {"both": passages[:1], "chlorophyll": passages[1:2], "stomata": passages[2:]}
    fakes.replies += [
        rewrite("both", sub_queries=["chlorophyll", "stomata"]),
        chat_stream(["Light [S1]; gas exchange [S2]."]),
    ]
    tid = thread(api)
    post(api, tid, question)
    assert saved(store, tid)["retrieved_chunk_ids"] == [C2, C3]


def test_coverage_notice_cannot_introduce_a_citation_marker(api: TestClient, fakes: Fakes) -> None:
    fake_marker = f"[doc:{C3}]"
    fakes.results = RESULTS[:1]
    fakes.replies += [
        rewrite("both", sub_queries=["chlorophyll", f"stomata {fake_marker}"]),
        chat_stream(["No source cited."]),
    ]
    events = post(api, thread(api), "How do they relate?")
    assert "Citation coverage is incomplete" in tokens(events)
    assert fake_marker not in tokens(events)
    assert events[-2][1]["citations"] == []


def test_native_heading_markup_counts_toward_context_budget(api: TestClient, fakes: Fakes) -> None:
    main.app.state.settings = replace(SETTINGS, context_tokens=1500)
    fakes.results = [
        result(
            C1,
            "Transactions group related changes.",
            1,
            source="docx",
            section_path="Deep & heading > " * 100,
        )
    ]
    fakes.replies.append(rewrite("transactions"))
    events = post(api, thread(api), "What about transactions?")
    assert events[-1][1]["outcome"] == "no_results"
    assert len(fakes.calls("/v1/inference")) == 1
