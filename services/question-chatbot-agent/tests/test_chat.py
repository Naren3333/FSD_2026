"""A chat turn end to end against the fakes: rewrite, search with the user's token, streamed and cited answer, and
what is saved. Ported from fsd_test's assistant tests."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
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
    assert set(citations[0]) == {"chunk_id", "document_id", "title", "page_start", "page_end", "kind", "source",
                                 "content", "image"}  # fmt: skip
    answer = saved(store, tid)
    assert events[-1][1] == {"message_id": answer["id"], "outcome": "answered"}
    assert (answer["outcome"], answer["prompt_version"], answer["model"], answer["usage_tokens"]) == (
        "answered", "student_tutor.v2", "gpt-test", 42)  # fmt: skip
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
        and rewrite_call["schema_name"] == "query_rewrite.v1"
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


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.Response(500), httpx.ConnectError("down")])
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
