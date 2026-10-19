"""Actual gateway, PostgreSQL, object storage, Kafka and authorization integration.

Creates uniquely named test data; never drops databases or deletes user data.
Runs against the disposable test stack (`python scripts/stack_tests.py integration` starts it, runs this and removes
it); --dev runs it against the development stack's data instead. Requires .env and httpx. Assertions are never
mocked.

main() runs the steps below in order, printing each one as it goes. Each step takes the signed-in users and what
earlier steps created. Without provider keys the AI paths may answer "unavailable"; `--live-ai` requires every one
of them to work instead, which makes a few small paid calls billed to the test users.
"""

import argparse
import contextlib
import dataclasses
import functools
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID, uuid4

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from local_stack import (
    BASE,
    ROOT,
    TOKEN_URL,
    admin_client,
    delete_topics,
    ensure_person,
    env,
    password_token,
    psql,
    psql_value,
    require_test_stack,
    sse_events,
)
from setup import AI_CLIENTS, SERVICES, client_secret_key

# gateway path → contract file
CONTRACTS = {"chatbot": "question-chatbot-agent", "question-generation": "question-generation-agent"}
TEXT = {"Content-Type": "text/plain"}
PDF = {"Content-Type": "application/pdf"}

T = TypeVar("T")
DUE_SECONDS = 8  # how far ahead of publishing the lifecycle step's due date is set
SCHEME_PASSAGE = "Full accuracy needs light energy, carbon dioxide and water named as the inputs."
LIVE_AI = False  # --live-ai: the AI paths must work, not just answer "unavailable"


# --- Helpers -------------------------------------------------------------------------------------------------


def docker(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        input=input,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=check,
        timeout=90,
    )


def sql_value(service: str, sql: str) -> str:
    """The single value a statement returns in a service's own database, as its own database user."""
    return psql_value(sql, database=service, user=service)


def login(username: str, password_key: str) -> httpx.Client:
    token = password_token(username, env()[password_key])
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + token}, timeout=25)


def service_token(client_id: str) -> str:
    secret = env()[client_secret_key(client_id)]
    data = {"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"}
    res = httpx.post(TOKEN_URL, data=data, timeout=15)
    assert res.status_code == 200, f"Service account login failed for {client_id}: {res.status_code}"
    return res.json()["access_token"]


# Runs inside the ai-core container, which has no published port: POSTs a valid embed request with the token
# read from stdin and prints the HTTP status.
AI_CORE_PROBE = """
import json, sys, urllib.error, urllib.request
body = {
    "operation": "embed",
    "input": "fractions",
    "model_profile": "default",
    "billed_to": {"user_id": "integration-test", "org_id": "local-school", "role": "teacher"},
}
headers = {"Authorization": "Bearer " + sys.stdin.read().strip(), "Content-Type": "application/json"}
req = urllib.request.Request("http://localhost:8080/v1/inference", data=json.dumps(body).encode(), headers=headers)
try:
    print(urllib.request.urlopen(req, timeout=10).status)
except urllib.error.HTTPError as e:
    print(e.code)
"""


def ai_core_status(token: str) -> int:
    return int(docker("exec", "-T", "ai-core", "python", "-c", AI_CORE_PROBE, input=token).stdout.strip())


def request(client: httpx.Client, method: str, path: str, status: int = 200, **kwargs: Any) -> Any:
    """Sends a request through the gateway, checks its status, and validates the JSON response against the
    service's committed OpenAPI contract."""
    response = client.request(method, path, **kwargs)
    if response.status_code == 429 and status != 429 and "API rate limit exceeded" in response.text:
        # Kong allows each signed-in session (bearer token) 120 requests a minute; a step that makes many calls in
        # one session can reach it, so wait for the next minute and try once more.
        time.sleep(61 - time.time() % 60)
        response = client.request(method, path, **kwargs)
    assert response.status_code == status, (
        f"{method} {path}: expected {status}, got {response.status_code}: {response.text[:400]}"
    )
    data = response.json() if response.content else None
    if data is not None:
        validate_response(method, path, status, data)
    return data


def validate_response(method: str, path: str, status: int, data: Any) -> None:
    service, _, resource = path.lstrip("/").partition("/")
    contract_file = ROOT / f"contracts/http/{CONTRACTS.get(service, service)}.openapi.json"
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    route = "/v1/" + resource.split("?")[0]
    # Literal paths before templated ones, as Go's mux matches them: /v1/evaluations/mine is not /v1/evaluations/{id}.
    for template, methods in sorted(contract["paths"].items(), key=lambda item: item[0].count("{")):
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), route) and method.lower() in methods:
            response = methods[method.lower()]["responses"].get(str(status), {})
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if schema:
                resolved = {**schema, "components": contract.get("components", {})}
                Draft202012Validator(resolved, format_checker=FormatChecker()).validate(data)
            return
    raise AssertionError(
        f"{method} {route} is not in {contract_file.name}, so its response cannot be checked"
    )


def eventually(action: Callable[[], T], predicate: Callable[[T], bool], label: str) -> T:
    deadline = time.monotonic() + 60
    while True:
        value = action()
        if predicate(value):
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out: {label}; last saw {str(value)[:400]}")
        time.sleep(1)


def replay(service: str, topic: str, resource: str, fresh_id: bool = False) -> str:
    """Publishes an outbox event to Kafka again, under its original key, optionally with a new event id, and returns
    its event id."""
    resource = str(UUID(resource))  # a UUID generated by this test or returned by a verified API
    row = sql_value(service, f"SELECT body FROM outbox WHERE partition_key='{resource}' AND topic='{topic}'")
    event = json.loads(row)
    schema = json.loads((ROOT / f"contracts/events/{topic}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(event)
    if fresh_id:
        event["event_id"] = str(uuid4())
    publish(topic, resource, event)
    return event["event_id"]


def publish(topic: str, key: str, event: dict[str, Any]) -> None:
    """Puts an event straight on a Kafka topic under `key`, as the outbox relay does (its partition key), so it
    keeps its order with the other events for the same resource."""
    producer = "/opt/kafka/bin/kafka-console-producer.sh"
    docker(
        "exec", "-T", "kafka", producer, "--bootstrap-server", "kafka:9092", "--topic", topic,
        "--property", "parse.key=true", "--property", "key.separator=|",
        input=f"{key}|{json.dumps(event)}\n",
    )  # fmt: skip


def restart(*services: str) -> None:
    """Restarts containers and waits until every application is ready again, so the next request is not a 502."""
    docker("restart", *services)
    ready = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "wait_ready.py")],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert ready.returncode == 0, (
        f"Not ready after restarting {', '.join(services)}:\n{ready.stdout}{ready.stderr}"
    )


def inbox_has(service: str, event_id: str) -> bool:
    """Whether a consumer has durably processed an event."""
    return sql_value(service, f"SELECT count(*) FROM inbox WHERE event_id='{UUID(event_id)}'") == "1"


# --- Steps ---------------------------------------------------------------------------------------------------


@dataclass
class Users:
    teacher: httpx.Client
    student: httpx.Client
    other: httpx.Client  # a teacher of another classroom
    teacher_id: str
    student_id: str


def sign_in() -> Users:
    teacher = login("teacher", "TEACHER_PASSWORD")
    student = login("student", "STUDENT_PASSWORD")
    other = login("other-teacher", "OTHER_TEACHER_PASSWORD")
    teacher_profile = request(teacher, "POST", "/identity/me", json={})
    student_profile = request(student, "POST", "/identity/me", json={})
    request(other, "POST", "/identity/me", json={})
    # Names come from the sign-in token.
    assert student_profile["username"] == "student", student_profile
    assert student_profile["display_name"] == "Student Local", student_profile
    return Users(teacher, student, other, teacher_profile["id"], student_profile["id"])


def classroom(u: Users) -> dict[str, Any]:
    """Creates a classroom and enrolls the student by username. Only the classroom's teacher sees its roster."""
    assert httpx.get(BASE + "/identity/classrooms").status_code == 401
    room = request(
        u.teacher, "POST", "/identity/classrooms", 201, json={"name": f"Integration {uuid4().hex[:8]}"}
    )
    CREATED_CLASSROOMS.append(room["id"])
    request(u.student, "POST", "/identity/classrooms", 403, json={"name": "Forbidden"})
    request(u.other, "GET", f"/identity/classrooms/{room['id']}/access", 403)

    enroll = f"/identity/classrooms/{room['id']}/enrollments"
    request(u.teacher, "POST", enroll, 422, json={"username": f"nobody-{uuid4().hex[:8]}"})
    request(u.other, "POST", enroll, 403, json={"username": "student"})
    enrolled = request(u.teacher, "POST", enroll, json={"username": "STUDENT"})
    assert enrolled["student_id"] == u.student_id

    roster = f"/identity/classrooms/{room['id']}/students"
    expected = [{"id": u.student_id, "username": "student", "display_name": "Student Local"}]
    assert request(u.teacher, "GET", roster) == {"students": expected, "total": 1}
    assert request(u.teacher, "GET", f"{roster}?offset=1&limit=500") == {"students": [], "total": 1}
    request(u.teacher, "GET", f"{roster}?limit=501", 422)
    request(u.student, "GET", roster, 403)
    request(u.other, "GET", roster, 403)
    # The id-only roster Assessment and Grading filter by has every student, unpaged.
    ids = f"/identity/classrooms/{room['id']}/student-ids"
    assert request(u.teacher, "GET", ids) == [u.student_id]
    request(u.student, "GET", ids, 403)

    # Teachers page through every student in their school, by search, to enroll them; students cannot.
    found = request(u.teacher, "GET", "/identity/students?search=STUDENT%20local&limit=50")
    assert expected[0] in found["students"] and found["total"] >= 1
    first = request(u.teacher, "GET", "/identity/students?limit=1")
    assert len(first["students"]) == 1 and first["total"] >= found["total"]
    request(u.teacher, "GET", "/identity/students?limit=51", 422)
    request(u.student, "GET", "/identity/students", 403)
    return room


def documents(u: Users, room_id: str) -> None:
    """Uploads, extraction, PDF indexing jobs, teaching material only, and deleting a document."""
    documents = f"/content/documents?classroom_id={room_id}"
    text = b"A fraction is a part of a whole."
    doc = request(u.teacher, "POST", f"{documents}&title=Integration", 201, content=text, headers=TEXT)
    assert doc["status"] == "extracted" and doc["chunks"][0]["text"] == text.decode()
    request(u.other, "GET", f"/content/documents/{doc['id']}", 403)
    request(u.student, "GET", f"/content/documents/{doc['id']}")
    original = u.student.get(f"/content/documents/{doc['id']}/original")
    assert original.status_code == 200 and original.content == text

    pdf = request(u.teacher, "POST", f"{documents}&title=PDF", 201, content=b"%PDF-1.7\n", headers=PDF)
    assert pdf["status"] == "indexing" and pdf["doc_type"] == "teaching_material"
    pdf_path = f"/content/documents/{pdf['id']}"

    # The indexing worker sends the PDF to ai-core: without provider keys the job waits to be retried; with
    # them, this header-only file fails as CORRUPT. Neither invents chunks.
    def indexing_state() -> str:
        job = sql_value(
            "content",
            f"SELECT attempts||' '||coalesce(last_error,'') FROM index_jobs WHERE document_id='{UUID(pdf['id'])}'",
        )
        return job or request(u.teacher, "GET", pdf_path)["status"]

    state = eventually(
        indexing_state, lambda v: "AI_PROVIDER_NOT_CONFIGURED" in v or v == "failed", "PDF indexing attempt"
    )
    assert state == "failed" or not LIVE_AI, f"PDF indexing did not reach ai-core's provider: {state}"
    if state == "failed":
        assert request(u.teacher, "GET", pdf_path)["failure_reason"] == "CORRUPT"
    else:
        request(u.teacher, "POST", f"{pdf_path}/reindex", 409)
    chunks = sql_value(
        "content", f"SELECT count(*) FROM document_chunks WHERE document_id='{UUID(pdf['id'])}'"
    )
    assert chunks == "0"
    request(u.student, "POST", f"{pdf_path}/reindex", 403)
    request(u.teacher, "POST", f"/content/documents/{doc['id']}/reindex", 409)
    request(u.teacher, "GET", f"{pdf_path}/figures/0123456789abcdef.png", 404)

    # Teachers upload teaching material only: rubrics and marking schemes are retired (ADR 0009).
    for doc_type in ["rubric", "marking_scheme", "answers"]:
        request(
            u.teacher, "POST", f"{documents}&title=Bad&doc_type={doc_type}", 422, content=b"x", headers=TEXT
        )

    # Deleting needs the classroom's teacher; it removes the row, its chunks and indexing job, and the original, and
    # emits content.deleted.v1 once. Its indexing job may still be retrying. (Question generation uses "PDF" later.)
    doomed = request(u.teacher, "POST", f"{documents}&title=Doomed", 201, content=b"%PDF-1.7\n", headers=PDF)
    pdf_path = f"/content/documents/{doomed['id']}"
    request(u.student, "DELETE", pdf_path, 403)
    request(u.other, "DELETE", pdf_path, 403)
    request(u.teacher, "DELETE", pdf_path, 204)
    request(u.teacher, "GET", pdf_path, 404)
    assert u.teacher.get(f"{pdf_path}/original").status_code == 404
    assert doomed["id"] not in [d["id"] for d in request(u.teacher, "GET", documents)]
    left = sql_value(
        "content",
        f"SELECT (SELECT count(*) FROM index_jobs WHERE document_id='{UUID(doomed['id'])}')||' '||"
        f"(SELECT count(*) FROM outbox WHERE topic='content.deleted.v1' AND partition_key='{UUID(doomed['id'])}')",
    )
    assert left == "0 1", f"jobs and deleted events after the delete: {left}"
    request(u.teacher, "DELETE", pdf_path, 404)

    # PDFs may be up to 50 MiB through the gateway; text stays at 5 MiB, refused by Content itself.
    request(u.teacher, "POST", f"{documents}&title=Big", 413, content=b"a" * (7 << 20), headers=TEXT)


def search(u: Users, room_id: str) -> None:
    """The caller's own classroom access, then ai-core for the query vector (503 without provider keys)."""
    query = {"classroom_id": room_id, "query": "What is a fraction?"}
    request(u.other, "POST", "/content/search", 403, json=query)
    request(u.student, "POST", "/content/search", 422, json={**query, "query": " "})
    searched = u.student.post("/content/search", json=query)
    assert searched.status_code in ((200,) if LIVE_AI else (200, 503)), searched.text
    if searched.status_code == 503:
        assert searched.json()["error"]["code"] == "SEARCH_UNAVAILABLE"
    else:
        validate_response("POST", "/content/search", 200, searched.json())


def chat(u: Users, room_id: str) -> None:
    """A thread needs classroom access; each turn searches with the user's own token and streams an answer from
    ai-core (an AI_UNAVAILABLE error event without provider keys, never a made-up answer)."""
    assert httpx.post(BASE + "/chatbot/threads", json={"classroom_id": room_id}).status_code == 401
    request(u.other, "POST", "/chatbot/threads", 403, json={"classroom_id": room_id})
    thread = request(u.student, "POST", "/chatbot/threads", 201, json={"classroom_id": room_id})
    messages = f"/chatbot/threads/{thread['id']}/messages"

    question = {"content": "What is a fraction?"}
    with u.student.stream("POST", messages, json=question, timeout=60) as streamed:
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        events = [name for name, _ in sse_events(streamed.iter_lines())]
    assert events[-1] in ("done", "error"), events

    turn = request(u.student, "GET", messages)["messages"]
    assert [m["role"] for m in turn] == ["user", "assistant"] and turn[0]["content"] == question["content"]
    assert turn[1]["outcome"] in ("answered", "no_results", "error")
    if LIVE_AI:
        # Without Voyage, search cannot rerank, so the relevance floor cannot be checked and the turn ends in error.
        if env().get("VOYAGE_API_KEY"):
            assert events[-1] == "done" and turn[1]["outcome"] in ("answered", "no_results"), (
                events,
                turn[1],
            )
        else:
            print("  note: chat answer not checked: VOYAGE_API_KEY is not set")
    request(u.other, "GET", messages, 404)
    request(u.teacher, "GET", messages, 404)
    threads = request(u.student, "GET", f"/chatbot/threads?classroom_id={room_id}")["threads"]
    assert [t["id"] for t in threads] == [thread["id"]]

    # Each user's AI spend today against their daily allowance (ADR 0003): $5 for a student, $10 for a teacher.
    assert request(u.student, "GET", "/chatbot/usage")["limit_usd"] == 5.0
    assert request(u.teacher, "GET", "/chatbot/usage")["limit_usd"] == 10.0
    assert httpx.get(BASE + "/chatbot/usage").status_code == 401


def question_generation(u: Users, room_id: str) -> None:
    """Only the classroom's teacher may generate questions or marking guidance (ADR 0006). The search runs with the
    teacher's own token over the chosen document; this classroom's PDF has no indexed passages, so the agent refuses
    with INSUFFICIENT_SOURCE (or SEARCH_UNAVAILABLE without provider keys) before any model call, never with made-up
    questions."""
    topic = new_topic(u.teacher, "Integration question generation")
    pdf = next(
        d
        for d in request(u.teacher, "GET", f"/content/documents?classroom_id={room_id}")
        if d["title"] == "PDF"
    )
    body = {
        "classroom_id": room_id,
        "document_ids": [pdf["id"]],
        "topic_id": topic["id"],
        "question_types": ["multiple_choice", "short_answer"],
        "difficulty": "introductory",
        "count": 2,
    }
    guidance = {
        "classroom_id": room_id,
        "document_ids": [pdf["id"]],
        "prompt": "What is a fraction?",
        "type": "short_answer",
    }
    for path, sent in (
        ("/question-generation/questions", body),
        ("/question-generation/marking-guidance", guidance),
    ):
        assert httpx.post(BASE + path, json=sent).status_code == 401
        request(u.student, "POST", path, 403, json=sent)
        request(u.other, "POST", path, 403, json=sent)
        request(u.teacher, "POST", path, 422, json={**sent, "document_ids": []})
        answered = u.teacher.post(path, json=sent, timeout=120)
        expected = (
            {422: "INSUFFICIENT_SOURCE"}
            if LIVE_AI
            else {422: "INSUFFICIENT_SOURCE", 503: "SEARCH_UNAVAILABLE"}
        )
        assert answered.status_code in expected, answered.text
        assert answered.json()["error"]["code"] == expected[answered.status_code], answered.text
        validate_response("POST", path, answered.status_code, answered.json())
    unknown = request(
        u.teacher, "POST", "/question-generation/questions", 422, json={**body, "topic_id": room_id}
    )
    assert unknown["error"]["code"] == "UNKNOWN_TOPIC", unknown


@dataclass
class Graded:
    submission_id: str
    grade_id: str
    performance_path: str
    topics: list[dict[str, Any]]  # the student's performance after finalization


def assess_and_grade(u: Users, room_id: str) -> Graded:
    """Answer-key secrecy, one submission, draft grading, teacher finalization, the student's own results and
    analytics."""
    topic = new_topic(u.teacher, "Integration exact decimals")
    questions = [
        {"prompt": "0.1 + 0.2?", "type": "numerical", "answer": "0.3", "tolerance": "0", "options": []},
        {
            "prompt": "Which is a fraction?",
            "type": "multiple_choice",
            "answer": "1/2",
            "tolerance": "",
            "options": ["1/2", "word"],
        },
    ]
    assessment = request(
        u.teacher, "POST", "/assessment/assessments", 201,
        json={
            "classroom_id": room_id,
            "title": "Decimal test",
            "questions": [{**q, "topic_id": topic["id"]} for q in questions],
        },
    )  # fmt: skip
    public = request(u.student, "GET", f"/assessment/assessments/{assessment['id']}")
    assert all("answer" not in q and "tolerance" not in q for q in public["questions"])

    # The first answer is right, the second wrong. A second submission is refused.
    first, second = (q["id"] for q in assessment["questions"])
    submissions = f"/assessment/assessments/{assessment['id']}/submissions"
    listed = f"/assessment/assessments?classroom_id={room_id}"
    entry = {
        "id": assessment["id"],
        "title": "Decimal test",
        "status": "published",
        "question_count": 2,
        "points": 2,
    }
    assert {**entry, "submitted": False} in summaries(request(u.student, "GET", listed), entry)
    answers = {"answers": {first: "0.30", second: "word"}}
    own = f"/assessment/assessments/{assessment['id']}/submission"
    request(u.student, "GET", own, 404)
    submission = request(u.student, "POST", submissions, 201, json=answers)
    assert submission["late"] is False
    request(u.student, "POST", submissions, 409, json=answers)
    own_submission(u, own, answers["answers"])
    # The list says who is asking has submitted: the student has, the teacher has not.
    assert {**entry, "submitted": True} in summaries(request(u.student, "GET", listed), entry)
    assert {**entry, "submitted": False} in summaries(request(u.teacher, "GET", listed), entry)

    evaluations = f"/grading/evaluations?classroom_id={room_id}"
    grades = eventually(
        lambda: request(u.teacher, "GET", evaluations), lambda page: page["total"] == 1, "draft grading"
    )
    grade = grades["evaluations"][0]
    assert grade["status"] == "draft" and sum(e["awarded"] for e in grade["evidence"]) == 1
    grading_pages(u, evaluations, grade["id"])

    # A student sees only their own finalized results: the draft stays with the teacher.
    mine = f"/grading/evaluations/mine?classroom_id={room_id}"
    assert request(u.student, "GET", mine) == []
    request(u.teacher, "GET", mine, 403)
    request(u.other, "GET", mine, 403)
    performance = f"/analytics/performance/{u.student_id}?classroom_id={room_id}"
    assert request(u.teacher, "GET", performance)["topics"] == []

    # Only the classroom's teacher finalizes, once.
    finalize = f"/grading/evaluations/{grade['id']}/finalize"
    request(u.student, "POST", finalize, 403, json={"reason": "Forbidden", "overrides": {}})
    request(u.other, "POST", finalize, 403, json={"reason": "Forbidden", "overrides": {}})
    request(u.teacher, "POST", finalize, json={"reason": "Integration teacher review", "overrides": {}})
    request(u.teacher, "POST", finalize, 409, json={"reason": "Duplicate", "overrides": {}})

    results = request(u.student, "GET", mine)
    assert [r["id"] for r in results] == [grade["id"]]
    assert results[0]["status"] == "finalized" and results[0]["finalized_at"]
    marked = [(e["question_id"], e["correct"]) for e in results[0]["evidence"]]
    assert marked == [(first, True), (second, False)]

    result = eventually(
        lambda: request(u.teacher, "GET", performance),
        lambda r: len(r["topics"]) == 1,
        "analytics consumption",
    )
    observed = result["topics"][0]
    assert observed["attempted"] == 2 and observed["correct"] == 1 and observed["observed_accuracy"] == 0.5
    assert observed["evidence_status"] == "insufficient_evidence"
    request(u.student, "GET", f"/analytics/performance/{u.teacher_id}?classroom_id={room_id}", 403)
    request(u.other, "GET", performance, 403)
    return Graded(submission["id"], grade["id"], performance, result["topics"])


def own_submission(u: Users, path: str, answers: dict[str, str]) -> None:
    """The student reads their own submitted answers again, never the answer keys; the teacher has them in
    Grading."""
    submitted = request(u.student, "GET", path)
    assert submitted["answers"] == answers and submitted["late"] is False and submitted["submitted_at"]
    request(u.teacher, "GET", path, 403)


def grading_pages(u: Users, evaluations: str, grade_id: str) -> None:
    """The teacher's grade list comes in pages, with how many there are; one grade is read again by its id, by the
    classroom's teacher only."""
    assert request(u.teacher, "GET", f"{evaluations}&offset=1") == {"evaluations": [], "total": 1}
    request(u.teacher, "GET", f"{evaluations}&limit=101", 422)
    one = f"/grading/evaluations/{grade_id}"
    assert request(u.teacher, "GET", one)["id"] == grade_id
    request(u.student, "GET", one, 403)
    request(u.other, "GET", one, 403)


def summaries(rows: list[dict[str, Any]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The assessment list's rows cut down to entry's fields and `submitted`."""
    return [{k: row[k] for k in [*entry, "submitted"]} for row in rows]


def replayed_events(u: Users, room_id: str, graded: Graded) -> None:
    """Consumers survive a restart, and replayed or re-identified events change nothing."""
    restart("grading", "analytics")
    # Each same-id copy is followed by a fresh-id copy under the same key. Kafka keeps a key's order, so once the
    # consumer has recorded the fresh copy it has also handled the same-id one before it.
    replay("assessment", "assessment.submitted.v4", graded.submission_id)
    submitted = replay("assessment", "assessment.submitted.v4", graded.submission_id, fresh_id=True)
    replay("grading", "grading.finalized.v3", graded.grade_id)
    finalized = replay("grading", "grading.finalized.v3", graded.grade_id, fresh_id=True)
    eventually(lambda: inbox_has("grading", submitted), bool, "grading handled the replayed submissions")
    eventually(
        lambda: inbox_has("analytics", finalized), bool, "analytics handled the replayed finalizations"
    )
    assert request(u.teacher, "GET", graded.performance_path)["topics"] == graded.topics
    assert request(u.teacher, "GET", f"/grading/evaluations?classroom_id={room_id}")["total"] == 1


def class_dashboard(u: Users, room_id: str, graded: Graded) -> None:
    """The class dashboard's numbers, for the classroom's teacher only: submissions per assessment, grades waiting
    and finalized, and one page of enrolled students with their topic evidence."""
    query = f"?classroom_id={room_id}"
    progress = f"/assessment/assessments/progress{query}"
    assert {"title": "Decimal test", "submissions": 1} in [
        {"title": a["title"], "submissions": a["submissions"]} for a in request(u.teacher, "GET", progress)
    ]
    counts = f"/grading/evaluations/counts{query}"
    assert request(u.teacher, "GET", counts) == {"drafts": 0, "finalized": 1, "unreleased": 0}
    performance = f"/analytics/performance{query}&limit=50"
    page = request(u.teacher, "GET", performance)
    assert page["total"] >= 1 and page["offset"] == 0
    [student] = [s for s in page["students"] if s["student_id"] == u.student_id]
    assert student["topics"] == graded.topics and student["evidence_status"] == "available"
    request(u.teacher, "GET", f"/analytics/performance{query}&limit=51", 422)
    for path in [progress, counts, performance]:
        request(u.student, "GET", path, 403)
        request(u.other, "GET", path, 403)


@dataclass
class Late:
    assessment_id: str
    topic_id: str
    short: str  # the short-answer question
    free: str  # the free-response question
    answers: dict[str, str]


def lifecycle(u: Users, room_id: str) -> Late:
    """Drafts stay with the teacher until published. A published assessment changes or is deleted only until a student
    submits it; then only its details change. Students save work, and submit after the due date, flagged late."""
    # Fresh sessions start with Kong's full allowance, so no rate-limit wait can pass the due date early.
    u = dataclasses.replace(
        u, teacher=login("teacher", "TEACHER_PASSWORD"), student=login("student", "STUDENT_PASSWORD")
    )
    topic = new_topic(u.teacher, "Integration photosynthesis")
    rubric = {
        "criteria": [
            {
                "title": "Accuracy",
                "levels": [
                    {"points": 0, "descriptor": "Wrong"},
                    {"points": 2, "descriptor": "Partly right"},
                    {"points": 4, "descriptor": "Right"},
                ],
            },
            {
                "title": "Clarity",
                "levels": [{"points": 0, "descriptor": "Unclear"}, {"points": 1, "descriptor": "Clear"}],
            },
        ],
        "model_answer": "Plants use light energy to turn carbon dioxide and water into glucose and oxygen.",
    }
    questions = [
        {
            "prompt": "Which gas do plants give off?",
            "type": "short_answer",
            "accepted_answers": ["oxygen"],
            "points": 2,
        },
        {
            "prompt": "Explain photosynthesis.",
            "type": "free_response",
            "rubric": rubric,
            "max_length": 2000,
            # A pasted marking-scheme passage, for AI suggestions only (ADR 0005).
            "marking_scheme": [{"text": SCHEME_PASSAGE}],
        },
    ]
    # Publishing needs a due date still ahead, and the late submission one already past. The draft starts with a due
    # date a day away; the last edit before publishing moves it DUE_SECONDS ahead, so only that edit and the publish
    # run against the clock, however slow the machine.
    draft = {
        "classroom_id": room_id,
        "title": "Photosynthesis draft",
        "status": "draft",
        "questions": [{**q, "topic_id": topic["id"]} for q in questions],
        "due_at": timestamp(time.time() + 86400),
        "late_policy": "accept_flagged",
        "release_policy": "manual",
        "ai_marking": True,
    }
    blank_passage = [{**questions[1], "topic_id": topic["id"], "marking_scheme": [{"text": " "}]}]
    request(u.teacher, "POST", "/assessment/assessments", 422, json={**draft, "questions": blank_passage})
    created = request(u.teacher, "POST", "/assessment/assessments", 201, json=draft)
    path = f"/assessment/assessments/{created['id']}"
    assert created["status"] == "draft" and created["questions"][1]["points"] == 5
    listed = f"/assessment/assessments?classroom_id={room_id}"
    # A draft is the teacher's alone: students neither see nor open it.
    assert created["id"] not in [a["id"] for a in request(u.student, "GET", listed)]
    request(u.student, "GET", path, 404)
    request(u.other, "PUT", path, 403, json={**draft, "title": "Forbidden"})
    spare = request(u.teacher, "POST", "/assessment/assessments", 201, json={**draft, "title": "Spare draft"})
    request(u.teacher, "DELETE", f"/assessment/assessments/{spare['id']}")
    request(u.teacher, "GET", f"/assessment/assessments/{spare['id']}", 404)

    due = time.time() + DUE_SECONDS
    edited = request(
        u.teacher, "PUT", path, json={**draft, "title": "Photosynthesis", "due_at": timestamp(due)}
    )
    assert edited["title"] == "Photosynthesis"
    published = request(u.teacher, "POST", f"{path}/publish")
    assert published["status"] == "published"
    request(u.teacher, "POST", f"{path}/publish", 409)
    request(u.teacher, "PUT", path, 409, json=draft)  # never a draft again
    renamed = still_changeable(u, path, {**draft, "status": "published", "due_at": timestamp(due)}, published)
    public = request(u.student, "GET", path)
    secrets = ["answer", "accepted_answers", "rubric", "marking_scheme"]
    assert all(k not in q for q in public["questions"] for k in secrets)
    assert SCHEME_PASSAGE not in json.dumps(public)
    assert published["questions"][1]["marking_scheme"] == [{"text": SCHEME_PASSAGE}]
    short, free = (q["id"] for q in published["questions"])

    # Saved work comes back until it is submitted.
    saved = f"{path}/saved-answers"
    request(u.student, "PUT", saved, json={"answers": {free: "Plants make"}})
    assert request(u.student, "GET", saved)["answers"] == {free: "Plants make"}
    request(u.teacher, "GET", saved, 403)
    time.sleep(max(0.0, due - time.time()) + 1)
    answers = {short: "O2 gas", free: "Plants use light to make glucose and give off oxygen."}
    submission = request(u.student, "POST", f"{path}/submissions", 201, json={"answers": answers})
    assert submission["late"] is True
    assert request(u.student, "GET", saved)["answers"] == {}
    only_details_change(u, path, {**draft, "status": "published"}, due, renamed)
    return Late(created["id"], topic["id"], short, free, answers)


def still_changeable(u: Users, path: str, again: dict[str, Any], published: dict[str, Any]) -> dict[str, Any]:
    """Until a student submits, a published assessment can still be replaced (unchanged questions keep their ids) or
    deleted. Returns it as replaced."""
    renamed = request(u.teacher, "PUT", path, json={**again, "title": "Photosynthesis"})
    assert renamed["status"] == "published" and renamed["published_at"]
    assert [q["id"] for q in renamed["questions"]] == [q["id"] for q in published["questions"]]
    gone = request(u.teacher, "POST", "/assessment/assessments", 201, json={**again, "title": "Deleted"})
    request(u.teacher, "DELETE", f"/assessment/assessments/{gone['id']}")
    request(u.student, "GET", f"/assessment/assessments/{gone['id']}", 404)
    return renamed


def only_details_change(
    u: Users, path: str, again: dict[str, Any], due: float, renamed: dict[str, Any]
) -> None:
    """A submission fixes the questions and grading settings: only the title, due date and late policy change now."""
    request(u.teacher, "PUT", path, 409, json={**again, "title": "Too late"})
    request(u.teacher, "DELETE", path, 409)
    request(
        u.teacher, "PATCH", path, 422, json={"title": "Photosynthesis", "due_at": timestamp(time.time() - 60)}
    )
    details = {"title": "Photosynthesis (marked)", "due_at": timestamp(due), "late_policy": "accept_flagged"}
    changed = request(u.teacher, "PATCH", path, json=details)
    assert changed["title"] == details["title"] and changed["questions"] == renamed["questions"]
    request(u.student, "PATCH", path, 403, json=details)


def timestamp(seconds: float) -> str:
    """A Unix time as the APIs' UTC date-time, to the second."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def marking(u: Users, room_id: str, late: Late) -> None:
    """The teacher marks short answers and rubric questions (with AI suggestions queued, never applied),
    finalizes, and releases results by hand; analytics counts part marks."""
    short, free, answers = late.short, late.free, late.answers

    # Neither answer can be marked automatically: the short answer matches no accepted answer. Each gets an AI
    # suggestion, which waits for ai-core (without provider keys it stays pending with the reason).
    evaluations = f"/grading/evaluations?classroom_id={room_id}&assessment_id={late.assessment_id}"
    [grade] = eventually(
        lambda: request(u.teacher, "GET", evaluations)["evaluations"], lambda rows: len(rows) == 1, "draft"
    )
    one = f"/grading/evaluations/{grade['id']}"
    assert grade["late"] and grade["answers"] == answers and grade["release_policy"] == "manual"
    assert [e["needs_marking"] for e in grade["evidence"]] == [True, True]
    # The passage reached Grading with the question (assessment.submitted.v4), for the marker to read.
    passage = (
        f"SELECT questions->1->'marking_scheme'->0->>'text' FROM evaluations WHERE id='{UUID(grade['id'])}'"
    )
    assert sql_value("grading", passage) == SCHEME_PASSAGE
    tried = eventually(
        lambda: request(u.teacher, "GET", one),
        lambda v: all(
            e["suggestion"]["status"] != "pending" or e["suggestion"].get("reason") for e in v["evidence"]
        ),
        "AI marking attempt",
    )
    assert all(e["awarded"] == 0 and "marked_by" not in e for e in tried["evidence"])
    if LIVE_AI:
        ready = eventually(
            lambda: request(u.teacher, "GET", one),
            lambda v: all(e["suggestion"]["status"] != "pending" for e in v["evidence"]),
            "AI marking suggestions",
        )
        assert all(e["suggestion"]["status"] == "ready" for e in ready["evidence"]), ready["evidence"]
        assert ready["evidence"][1]["suggestion"]["scheme_passages"] == 1
        assert all(e["awarded"] == 0 and "marked_by" not in e for e in ready["evidence"])
    request(u.teacher, "GET", f"/grading/evaluations?classroom_id={room_id}&assessment_id=nope", 422)

    finalize = f"/grading/evaluations/{grade['id']}/finalize"
    request(u.teacher, "POST", finalize, 422, json={"reason": "Too early"})
    marks = f"/grading/evaluations/{grade['id']}/marks"
    request(u.teacher, "POST", marks, 422, json={"marks": {free: {"criteria": {"c1": 3, "c2": 1}}}})
    request(u.teacher, "POST", marks, 422, json={"marks": {short: {"awarded": 3}}})
    request(u.student, "POST", marks, 403, json={"marks": {short: {"awarded": 2}}})
    request(u.other, "POST", marks, 403, json={"marks": {short: {"awarded": 2}}})
    feedback = "Name the products and where the energy comes from."
    marked = request(
        u.teacher, "POST", marks,
        json={
            "marks": {
                short: {"awarded": 2, "feedback": "O2 is oxygen: accepted."},
                free: {"criteria": {"c1": 2, "c2": 1}, "feedback": feedback},
            }
        },
    )  # fmt: skip
    assert marked["status"] == "draft" and [e["awarded"] for e in marked["evidence"]] == [2, 3]
    request(u.teacher, "POST", finalize, json={"reason": "Marked by hand"})

    # Finalized but not released: the student sees nothing yet, in Grading or in Analytics. Grading publishes
    # grading.finalized.v3 only on release, so no event can reach Analytics later either.
    mine = f"/grading/evaluations/mine?classroom_id={room_id}"
    assert grade["id"] not in [r["id"] for r in request(u.student, "GET", mine)]
    performance = f"/analytics/performance/{u.student_id}?classroom_id={room_id}"
    unreleased = request(u.student, "GET", performance)["topics"]
    assert late.topic_id not in [s["topic_id"] for s in unreleased], unreleased
    finalized_events = (
        "SELECT count(*) FROM outbox WHERE topic='grading.finalized.v3' "
        f"AND partition_key='{UUID(grade['id'])}'"
    )
    assert sql_value("grading", finalized_events) == "0", "grading.finalized.v3 was published before release"
    counts = f"/grading/evaluations/counts?classroom_id={room_id}"
    assert request(u.teacher, "GET", counts) == {"drafts": 0, "finalized": 2, "unreleased": 1}
    # One assessment's counts are those its release acts on.
    assert request(u.teacher, "GET", f"{counts}&assessment_id={late.assessment_id}") == {
        "drafts": 0,
        "finalized": 1,
        "unreleased": 1,
    }
    release = "/grading/evaluations/release"
    request(u.student, "POST", release, 403, json={"assessment_id": late.assessment_id})
    request(u.other, "POST", release, 403, json={"assessment_id": late.assessment_id})
    assert request(u.teacher, "POST", release, json={"assessment_id": late.assessment_id})["released"] == 1
    assert request(u.teacher, "POST", release, json={"assessment_id": late.assessment_id})["released"] == 0
    [result] = [r for r in request(u.student, "GET", mine) if r["id"] == grade["id"]]
    assert [(e["awarded"], e["possible"]) for e in result["evidence"]] == [(2, 2), (3, 5)]
    assert result["evidence"][1]["feedback"] == feedback and result["late"]
    assert all("suggestion" not in e and "criteria" not in e for e in result["evidence"])

    observed = eventually(
        lambda: request(u.student, "GET", performance)["topics"],
        lambda topics: any(s["topic_id"] == late.topic_id for s in topics),
        "part marks in analytics",
    )
    [part] = [s for s in observed if s["topic_id"] == late.topic_id]
    assert (part["attempted"], part["correct"], part["marks_awarded"], part["marks_possible"]) == (2, 1, 5, 7)


def unenroll(u: Users, room_id: str) -> None:
    """Removing a student keeps their grades but ends their access to the classroom. Their work leaves the teacher's
    review list and counts, and Grading records the removal so their queued AI marking stops."""
    room = str(UUID(room_id))  # returned by a verified API
    student = u.student_id
    assert student.replace("-", "").isalnum(), student  # an account id, safe in SQL
    remove = f"/identity/classrooms/{room_id}/enrollments/{u.student_id}"
    request(u.other, "DELETE", remove, 403)
    request(u.student, "DELETE", remove, 403)
    request(u.teacher, "DELETE", remove)
    request(u.teacher, "DELETE", remove, 404)
    assert request(u.teacher, "GET", f"/identity/classrooms/{room_id}/students") == {
        "students": [],
        "total": 0,
    }
    request(u.student, "GET", f"/grading/evaluations/mine?classroom_id={room_id}", 403)
    assert room_id not in [r["id"] for r in request(u.student, "GET", "/identity/classrooms")]
    removed = (
        f"SELECT count(*) FROM removed_enrollments WHERE classroom_id='{room}' AND student_id='{student}'"
    )
    eventually(
        functools.partial(sql_value, "grading", removed), lambda value: value == "1", "removal recorded"
    )
    assert request(u.teacher, "GET", f"/grading/evaluations?classroom_id={room_id}") == {
        "evaluations": [],
        "total": 0,
    }
    kept = f"SELECT count(*) FROM evaluations WHERE classroom_id='{room}' AND student_id='{student}'"
    assert sql_value("grading", kept) == "2"
    progress = request(u.teacher, "GET", f"/assessment/assessments/progress?classroom_id={room_id}")
    assert all(a["submissions"] == 0 for a in progress), progress


def delete_account(u: Users, room_id: str) -> None:
    """A user deletes their own account (ADR 0011). A teacher with classrooms is refused. A throwaway student, with
    a chat thread in the classroom, deletes theirs: Identity refuses it from then on and removes its sign-in from
    Keycloak, and Assessment, Grading, Analytics and the chatbot purge it and remember it."""
    teacher = request(u.teacher, "DELETE", "/identity/me", 409)
    assert teacher["error"]["code"] == "OWNS_CLASSROOMS", teacher
    username, password = f"deleted-{uuid4().hex[:8]}", uuid4().hex
    with admin_client() as admin:
        ensure_person(admin, username, "Deleted", "Student", password)
    token = password_token(username, password)
    gone = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + token}, timeout=25)
    user_id = request(gone, "POST", "/identity/me", json={})["id"]
    user = str(UUID(user_id))  # Keycloak ids are UUIDs, so safe in SQL
    request(u.teacher, "POST", f"/identity/classrooms/{room_id}/enrollments", json={"username": username})
    request(gone, "POST", "/chatbot/threads", 201, json={"classroom_id": room_id})
    assert sql_value("chatbot", f"SELECT count(*) FROM threads WHERE user_id='{user}'") == "1"

    assert httpx.delete(BASE + "/identity/me").status_code == 401
    gone.request("DELETE", "/identity/me").raise_for_status()
    assert request(gone, "POST", "/identity/me", 403, json={})["error"]["code"] == "ACCOUNT_DELETED"
    request(gone, "DELETE", "/identity/me", 404)
    request(gone, "POST", "/chatbot/threads", 403, json={"classroom_id": room_id})
    assert sql_value("identity", f"SELECT count(*) FROM enrollments WHERE student_id='{user}'") == "0"
    assert sql_value("identity", f"SELECT coalesce(username,'') FROM users WHERE id='{user}'") == ""
    for service in ["assessment", "grading", "analytics", "chatbot"]:
        tombstone = f"SELECT count(*) FROM deleted_users WHERE user_id='{user}'"
        eventually(functools.partial(sql_value, service, tombstone), lambda v: v == "1", f"{service} purge")
    assert sql_value("chatbot", f"SELECT count(*) FROM threads WHERE user_id='{user}'") == "0"
    # Identity removes the sign-in account from Keycloak shortly after, so the password no longer signs in.
    signin = {
        "client_id": "learning-e2e",
        "grant_type": "password",
        "username": username,
        "password": password,
    }
    eventually(
        lambda: httpx.post(TOKEN_URL, data=signin, timeout=15).status_code,
        lambda code: code == 401,
        "sign-in",
    )
    removed = f"SELECT count(*) FROM users WHERE id='{user}' AND signin_removed_at IS NOT NULL"
    assert sql_value("identity", removed) == "1"


def delete_graded_assessment(u: Users, room_id: str, late: Late) -> None:
    """Deleting an assessment students submitted needs the teacher to confirm it also deletes their grades. Then
    Grading and Analytics purge it, and its submission arriving again grades nothing."""
    assessment = str(UUID(late.assessment_id))  # returned by a verified API
    kept = {
        "assessment": f"SELECT count(*) FROM submissions WHERE assessment_id='{assessment}'",
        "grading": f"SELECT count(*) FROM evaluations WHERE assessment_id='{assessment}'",
        "analytics": f"SELECT count(*) FROM evidence WHERE assessment_id='{assessment}'",
    }
    for service, sql in kept.items():
        assert sql_value(service, sql) != "0", service
    submitted_sql = (
        "SELECT body FROM outbox WHERE topic='assessment.submitted.v4' "
        f"AND body->'data'->>'assessment_id'='{assessment}' LIMIT 1"
    )
    submitted = json.loads(sql_value("assessment", submitted_sql))
    path = f"/assessment/assessments/{assessment}"
    request(u.student, "DELETE", f"{path}?with_submissions=true", 403)
    request(u.teacher, "DELETE", path, 409)
    request(u.teacher, "DELETE", f"{path}?with_submissions=true")
    request(u.teacher, "DELETE", f"{path}?with_submissions=true", 404)
    for service, sql in kept.items():
        eventually(functools.partial(sql_value, service, sql), lambda value: value == "0", f"{service} purge")
    graded = request(u.teacher, "GET", f"/grading/evaluations?classroom_id={room_id}")
    assert assessment not in [e["assessment_id"] for e in graded["evaluations"]], graded

    late_id = str(uuid4())
    publish("assessment.submitted.v4", submitted["data"]["submission_id"], {**submitted, "event_id": late_id})
    eventually(lambda: inbox_has("grading", late_id), bool, "late submission")
    for service, sql in kept.items():
        assert sql_value(service, sql) == "0", service


def delete_classroom(u: Users, room_id: str, graded: Graded) -> None:
    """Deleting a classroom hides it, unenrolls its students and ends everyone's access, its teacher's included.
    Then every service purges its data for the classroom, a repeated deletion changes nothing, and a submission
    event arriving after the purge grades nothing; the students' accounts are kept."""
    room = str(UUID(room_id))  # returned by a verified API
    kept = {
        "content": f"SELECT count(*) FROM documents WHERE classroom_id='{room}'",
        "assessment": f"SELECT count(*) FROM assessments WHERE classroom_id='{room}'",
        "grading": f"SELECT count(*) FROM evaluations WHERE classroom_id='{room}'",
        "analytics": f"SELECT count(*) FROM evidence WHERE classroom_id='{room}'",
        "chatbot": f"SELECT count(*) FROM threads WHERE classroom_id='{room}'",
    }
    for service, sql in kept.items():
        assert sql_value(service, sql) != "0", service
    submitted_sql = f"SELECT body FROM outbox WHERE partition_key='{UUID(graded.submission_id)}'"
    late_submission = json.loads(sql_value("assessment", submitted_sql))
    request(u.teacher, "POST", f"/identity/classrooms/{room_id}/enrollments", json={"username": "student"})
    path = f"/identity/classrooms/{room_id}"
    request(u.other, "DELETE", path, 403)
    request(u.student, "DELETE", path, 403)
    request(u.teacher, "DELETE", path)
    CREATED_CLASSROOMS.remove(room_id)
    request(u.teacher, "DELETE", path, 403)
    for user in [u.teacher, u.student]:
        request(user, "GET", f"{path}/access", 403)
        assert room_id not in [r["id"] for r in request(user, "GET", "/identity/classrooms")]
    request(u.teacher, "GET", f"/grading/evaluations?classroom_id={room_id}", 403)
    # Its students are unenrolled, so none stays tied to it.
    assert sql_value("identity", f"SELECT count(*) FROM enrollments WHERE classroom_id='{room_id}'") == "0"
    # The students themselves are kept and can still be enrolled elsewhere.
    assert request(u.teacher, "GET", "/identity/students?search=STUDENT%20local")["total"] >= 1

    for service, sql in kept.items():
        eventually(
            functools.partial(sql_value, service, sql),
            lambda value: value == "0",
            f"{service} purge",
        )
    for service in ["content", "assessment", "grading"]:
        unsent = f"SELECT count(*) FROM outbox WHERE body->'data'->>'classroom_id'='{room}'"
        assert sql_value(service, unsent) == "0", service
    # A repeated deletion and a late submission change nothing.
    duplicate_id = replay("identity", "identity.classroom.deleted.v1", room, fresh_id=True)
    late_id = str(uuid4())
    publish(
        "assessment.submitted.v4", str(UUID(graded.submission_id)), {**late_submission, "event_id": late_id}
    )
    eventually(
        lambda: sql_value("content", f"SELECT count(*) FROM inbox WHERE event_id='{duplicate_id}'"),
        lambda value: value == "1",
        "repeated deletion",
    )
    eventually(
        lambda: sql_value("grading", f"SELECT count(*) FROM inbox WHERE event_id='{late_id}'"),
        lambda value: value == "1",
        "late submission",
    )
    for service, sql in kept.items():
        assert sql_value(service, sql) == "0", service


def databases() -> None:
    """Each service's database user is refused by the others, and migrations and pgvector are in place."""
    for user in SERVICES:
        for database in SERVICES:
            if user != database:
                isolation = psql("SELECT 1", database=database, user=user, check=False)
                refused = isolation.returncode != 0 and "permission denied" in isolation.stderr.lower()
                assert refused, (user, database)
    # Exactly these migrations have been applied. The list is written out, not read from the migrations folders, so
    # a migration file deleted by mistake fails here; add each new migration's number to it.
    versions = "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations"
    for service, expected in [
        ("content", "1,2,3,4"),
        ("identity", "1,2,3,4,5,6,7,8"),
        ("assessment", "1,2,3,4,5"),
        ("grading", "1,2,3,4,5,6,7"),
        ("analytics", "1,2,3,4,5,6"),
        ("curriculum", "1,2"),
        ("chatbot", "1,2,3,4,5,6"),
        ("aicore", "1,2"),
    ]:
        migrations = sql_value(service, versions)
        assert migrations == expected, (service, migrations, expected)
    vector = sql_value("content", "SELECT count(*) FROM pg_extension WHERE extname='vector'")
    assert vector == "1", "pgvector is not installed in Content's database"


def ai_service_accounts(u: Users) -> None:
    """Service accounts reach the private AI service (200 with provider keys, 503 without); people do not."""
    for client_id in AI_CLIENTS:
        assert ai_core_status(service_token(client_id)) in ((200,) if LIVE_AI else (200, 503)), client_id
    assert ai_core_status(u.student.headers["Authorization"].removeprefix("Bearer ")) == 403


# Runs inside the analytics container: sends a valid request to each agent that is not authorized yet and prints
# its status and error code. AGENTS.md: they answer 501 until their implementation is authorized.
PLACEHOLDER_PROBE = """
import json, sys, urllib.error, urllib.request
token, room = sys.stdin.read().split()
ids = ["00000000-0000-4000-8000-000000000001"]
requests = {
    "student-performance-agent:8080/v1/performance_interpretation": {
        "student_id": "student", "classroom_id": room, "evidence_ids": ids, "topic_ids": ids,
        "performance_summary": [{"topic_id": ids[0], "attempted": 1, "correct": 1}],
    },
}
for path, body in requests.items():
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    req = urllib.request.Request("http://" + path, data=json.dumps(body).encode(), headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=10)
        print(path, response.status, "")
    except urllib.error.HTTPError as e:
        print(path, e.code, json.loads(e.read() or b"{}").get("error", {}).get("code", ""))
"""


def unauthorized_agents() -> None:
    """Performance interpretation still answers 501 to a valid, authorized request."""
    token = service_token("content-indexer")
    probe = docker("exec", "-T", "analytics", "python", "-c", PLACEHOLDER_PROBE, input=f"{token} {uuid4()}")
    lines = probe.stdout.split("\n")
    results = [line.split() for line in lines if line.strip()]
    assert len(results) == 1, probe.stdout
    for path, status, code in results:
        assert (status, code) == ("501", "AI_CAPABILITY_NOT_IMPLEMENTED"), (path, status, code)


def rate_limit_per_session() -> None:
    """Kong's rate limit is each signed-in session's own: one session using it up leaves others unaffected."""
    busy, other = login("teacher", "TEACHER_PASSWORD"), login("student", "STUDENT_PASSWORD")
    statuses = [busy.get("/identity/classrooms").status_code for _ in range(125)]
    assert statuses.count(429) > 0, "Kong did not rate limit a session making 125 requests in a minute"
    request(other, "GET", "/identity/classrooms")


# The topics this run created, deleted when it ends so test topics never pile up in the local curriculum.
CREATED_TOPICS: list[str] = []
# The classroom this run created, until its deletion step deletes it; a failed run deletes it on the way out.
CREATED_CLASSROOMS: list[str] = []


def new_topic(teacher: httpx.Client, name: str) -> dict[str, Any]:
    topic: dict[str, Any] = request(teacher, "POST", "/curriculum/topics", 201, json={"name": name})
    assert set(topic) == {"id", "name"}, topic
    CREATED_TOPICS.append(topic["id"])
    return topic


def delete_test_topics(topic_ids: list[str]) -> None:
    """Deletes test topics outright. Only for topics this run created: by now its classroom is purged, so nothing of
    the run still names them."""
    if topic_ids:
        delete_topics("id IN (" + ",".join(f"'{UUID(i)}'" for i in topic_ids) + ")")


def remove_topics() -> None:
    """Archiving moves a topic from the curriculum list to the archived list (a second archive is a 404), where a
    student can still read its name and only a teacher can restore it; then the run's topics are deleted."""
    teacher, student = login("teacher", "TEACHER_PASSWORD"), login("student", "STUDENT_PASSWORD")
    first = CREATED_TOPICS[0]

    def listed(archived: bool) -> set[str]:
        path = "/curriculum/topics?archived=true" if archived else "/curriculum/topics"
        return {t["id"] for t in request(teacher, "GET", path)}

    request(teacher, "DELETE", f"/curriculum/topics/{first}", 204)
    assert first not in listed(False) and first in listed(True), (
        "archived topic not moved to the archived list"
    )
    assert first in {t["id"] for t in request(student, "GET", "/curriculum/topics?archived=true")}
    request(teacher, "DELETE", f"/curriculum/topics/{first}", 404)
    request(student, "POST", f"/curriculum/topics/{first}/restore", 403)
    restored = request(teacher, "POST", f"/curriculum/topics/{first}/restore")
    assert restored == {"id": first, "name": "Integration question generation"}, restored
    assert first in listed(False) and first not in listed(True), "restored topic not active"
    request(teacher, "POST", f"/curriculum/topics/{first}/restore", 404)
    delete_test_topics(CREATED_TOPICS)
    assert sql_value("curriculum", f"SELECT count(*) FROM topics WHERE id='{UUID(first)}'") == "0"
    CREATED_TOPICS.clear()


def remove_leftovers() -> None:
    """After a failed run, deletes what it created without checking, so the failure stays the one reported. The
    classroom goes through Identity's API, so every service purges its data for it as for a teacher's deletion."""
    if CREATED_CLASSROOMS:
        with (
            contextlib.suppress(httpx.HTTPError, SystemExit),
            login("teacher", "TEACHER_PASSWORD") as teacher,
        ):
            for room_id in CREATED_CLASSROOMS:
                teacher.delete(f"/identity/classrooms/{room_id}")
    with contextlib.suppress(subprocess.SubprocessError, ValueError):
        delete_test_topics(CREATED_TOPICS)


def step[R](label: str, action: Callable[[], R]) -> R:
    """Runs one step and prints how long it took, so a slow or stuck run shows where it is."""
    print(f"{label}...", end=" ", flush=True)
    started = time.monotonic()
    result = action()
    print(f"ok ({time.monotonic() - started:.0f}s)", flush=True)
    return result


def main() -> None:
    global LIVE_AI
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--live-ai", action="store_true", help="require every AI path to work (paid calls)")
    parser.add_argument(
        "--dev", action="store_true", help="run against the development stack, not the test stack"
    )
    args = parser.parse_args()
    require_test_stack(dev=args.dev)
    LIVE_AI = args.live_ai
    if LIVE_AI and not env().get("OPENAI_API_KEY"):
        raise SystemExit("--live-ai needs OPENAI_API_KEY in .env")
    started = time.monotonic()
    try:
        run()
    finally:
        remove_leftovers()
    print(f"Finished in {time.monotonic() - started:.0f}s{' with live AI' if LIVE_AI else ''}.")
    print(
        "PASS: gateway auth, classroom isolation, enrollment by username, rosters, the school's student list, unenrollment, account deletion and its cross-service purge, classroom deletion and its cross-service purge, deleting a graded assessment with its grades, curriculum, "
        "S3 upload/extraction, PDF indexing jobs, document deletion, search access, chat threads and streaming, "
        "teacher-only question generation and marking guidance, "
        "AI spend allowance, answer-key secrecy, submissions, draft grading, teacher finalization, students' own "
        "finalized results, the class dashboard's counts and class performance, Kafka analytics, consumer restart, duplicate event and business-key idempotency, "
        "draft and scheduled assessments, saved answers, late submissions, short-answer and rubric marking, "
        "AI marking suggestions, manual release (no analytics before it), part marks in analytics, "
        "student privacy, database isolation, numbered migrations, pgvector, AI service accounts, 501 for unauthorized "
        "agents, per-session rate limiting."
    )


def run() -> None:
    users = step("Signing in", sign_in)
    room_id = step("Classroom and enrollment", lambda: classroom(users))["id"]
    step("Documents and indexing", lambda: documents(users, room_id))
    step("Search", lambda: search(users, room_id))
    step("Chat", lambda: chat(users, room_id))
    step("Question generation", lambda: question_generation(users, room_id))
    graded = step("Assessment and grading", lambda: assess_and_grade(users, room_id))
    step("Restart and replayed events", lambda: replayed_events(users, room_id, graded))
    step("Class dashboard", lambda: class_dashboard(users, room_id, graded))
    late = step("Assessment lifecycle", lambda: lifecycle(users, room_id))
    step("Marking and release", lambda: marking(users, room_id, late))
    step("Unenrollment", lambda: unenroll(users, room_id))
    step("Deleting an account", lambda: delete_account(users, room_id))
    step("Deleting a graded assessment", lambda: delete_graded_assessment(users, room_id, late))
    step("Classroom deletion and purge", lambda: delete_classroom(users, room_id, graded))
    step("Database isolation and migrations", databases)
    step("AI service accounts", lambda: ai_service_accounts(users))
    step("Unauthorized agents answer 501", unauthorized_agents)
    step("Per-session rate limit", rate_limit_per_session)
    step("Removing this run's topics", remove_topics)


if __name__ == "__main__":
    main()
