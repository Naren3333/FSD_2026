"""Actual gateway, PostgreSQL, object storage, Kafka and authorization integration.

Creates uniquely named test data; never drops databases or deletes user data.
Requires the local Compose stack, .env and httpx. Assertions are never mocked.

main() runs the steps below in order. Each step takes the signed-in users and what earlier steps created.
"""

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID, uuid4

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from local_stack import BASE, ROOT, TOKEN_URL, env, password_token, psql, psql_value, sse_events
from setup import client_secret_key

CONTRACTS = {"chatbot": "question-chatbot-agent"}  # gateway path → contract file
TEXT = {"Content-Type": "text/plain"}
PDF = {"Content-Type": "application/pdf"}

T = TypeVar("T")


# --- Helpers -------------------------------------------------------------------------------------------------


def docker(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        input=input,
        text=True,
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
        # Kong's limit of 120 requests a minute covers this whole run; wait for the next minute and try once more.
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
    contract = json.loads(contract_file.read_text())
    route = "/v1/" + resource.split("?")[0]
    for template, methods in contract["paths"].items():
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), route) and method.lower() in methods:
            response = methods[method.lower()]["responses"].get(str(status), {})
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if schema:
                resolved = {**schema, "components": contract.get("components", {})}
                Draft202012Validator(resolved, format_checker=FormatChecker()).validate(data)
            break


def eventually(action: Callable[[], T], predicate: Callable[[T], bool], label: str) -> T:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        value = action()
        if predicate(value):
            return value
        time.sleep(1)
    raise AssertionError(f"Timed out: {label}")


def replay(service: str, topic: str, resource: str, fresh_id: bool = False) -> str:
    """Publishes an outbox event to Kafka again, optionally under a new event id, and returns its event id."""
    resource = str(UUID(resource))  # a UUID generated by this test or returned by a verified API
    row = sql_value(service, f"SELECT body FROM outbox WHERE partition_key='{resource}' AND topic='{topic}'")
    event = json.loads(row)
    schema = json.loads((ROOT / f"contracts/events/{topic}.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(event)
    if fresh_id:
        event["event_id"] = str(uuid4())
    publish(topic, event)
    return event["event_id"]


def publish(topic: str, event: dict[str, Any]) -> None:
    """Puts an event straight on a Kafka topic."""
    producer = "/opt/kafka/bin/kafka-console-producer.sh"
    docker(
        "exec", "-T", "kafka", producer, "--bootstrap-server", "kafka:9092", "--topic", topic,
        input=json.dumps(event) + "\n",
    )  # fmt: skip


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
    request(u.student, "POST", "/identity/classrooms", 403, json={"name": "Forbidden"})
    request(u.other, "GET", f"/identity/classrooms/{room['id']}/access", 403)

    enroll = f"/identity/classrooms/{room['id']}/enrollments"
    request(u.teacher, "POST", enroll, 422, json={"username": f"nobody-{uuid4().hex[:8]}"})
    request(u.other, "POST", enroll, 403, json={"username": "student"})
    enrolled = request(u.teacher, "POST", enroll, json={"username": "STUDENT"})
    assert enrolled["student_id"] == u.student_id

    roster = f"/identity/classrooms/{room['id']}/students"
    expected = [{"id": u.student_id, "username": "student", "display_name": "Student Local"}]
    assert request(u.teacher, "GET", roster) == expected
    request(u.student, "GET", roster, 403)
    request(u.other, "GET", roster, 403)

    # Teachers page through every student in their school, by search, to enroll them; students cannot.
    found = request(u.teacher, "GET", "/identity/students?search=STUDENT%20local&limit=50")
    assert expected[0] in found["students"] and found["total"] >= 1
    first = request(u.teacher, "GET", "/identity/students?limit=1")
    assert len(first["students"]) == 1 and first["total"] >= found["total"]
    request(u.teacher, "GET", "/identity/students?limit=51", 422)
    request(u.student, "GET", "/identity/students", 403)
    return room


def documents(u: Users, room_id: str) -> None:
    """Uploads, extraction, PDF indexing jobs and teacher-only rubrics."""
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

    # Rubrics and marking schemes are for teachers only.
    rubric = request(
        u.teacher, "POST", f"{documents}&title=Rubric&doc_type=rubric", 201,
        content=b"Band 4: explains clearly.", headers=TEXT,
    )  # fmt: skip
    request(u.teacher, "POST", f"{documents}&title=Bad&doc_type=answers", 422, content=b"x", headers=TEXT)
    assert rubric["id"] in [d["id"] for d in request(u.teacher, "GET", documents)]
    assert rubric["id"] not in [d["id"] for d in request(u.student, "GET", documents)]
    request(u.student, "GET", f"/content/documents/{rubric['id']}", 404)
    assert u.student.get(f"/content/documents/{rubric['id']}/original").status_code == 404

    # PDFs may be up to 50 MiB through the gateway; text stays at 5 MiB, refused by Content itself.
    request(u.teacher, "POST", f"{documents}&title=Big", 413, content=b"a" * (7 << 20), headers=TEXT)


def search(u: Users, room_id: str) -> None:
    """The caller's own classroom access, then ai-core for the query vector (503 without provider keys)."""
    query = {"classroom_id": room_id, "query": "What is a fraction?"}
    request(u.other, "POST", "/content/search", 403, json=query)
    request(u.student, "POST", "/content/search", 422, json={**query, "query": " "})
    searched = u.student.post("/content/search", json=query)
    assert searched.status_code in (200, 503), searched.text
    if searched.status_code == 503:
        assert searched.json()["error"]["code"] == "SEARCH_UNAVAILABLE"
    else:
        request(u.student, "POST", "/content/search", json=query)


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
    request(u.other, "GET", messages, 404)
    request(u.teacher, "GET", messages, 404)
    threads = request(u.student, "GET", f"/chatbot/threads?classroom_id={room_id}")["threads"]
    assert [t["id"] for t in threads] == [thread["id"]]

    # Each user's AI spend today against their daily allowance (ADR 0003): $5 for a student, $10 for a teacher.
    assert request(u.student, "GET", "/chatbot/usage")["limit_usd"] == 5.0
    assert request(u.teacher, "GET", "/chatbot/usage")["limit_usd"] == 10.0
    assert httpx.get(BASE + "/chatbot/usage").status_code == 401


@dataclass
class Graded:
    submission_id: str
    grade_id: str
    performance_path: str
    skills: list[dict[str, Any]]  # the student's performance after finalization


def assess_and_grade(u: Users, room_id: str) -> Graded:
    """Answer-key secrecy, one submission, draft grading, teacher finalization, the student's own results and
    analytics."""
    skill = request(
        u.teacher, "POST", "/curriculum/nodes", 201,
        json={"kind": "skill", "name": "Integration exact decimals", "parent_id": None},
    )  # fmt: skip
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
            "questions": [{**q, "skill_id": skill["id"]} for q in questions],
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
    submission = request(u.student, "POST", submissions, 201, json=answers)
    assert submission["late"] is False
    request(u.student, "POST", submissions, 409, json=answers)
    # The list says who is asking has submitted: the student has, the teacher has not.
    assert {**entry, "submitted": True} in summaries(request(u.student, "GET", listed), entry)
    assert {**entry, "submitted": False} in summaries(request(u.teacher, "GET", listed), entry)

    evaluations = f"/grading/evaluations?classroom_id={room_id}"
    grades = eventually(
        lambda: request(u.teacher, "GET", evaluations), lambda rows: len(rows) == 1, "draft grading"
    )
    grade = grades[0]
    assert grade["status"] == "draft" and sum(e["awarded"] for e in grade["evidence"]) == 1

    # A student sees only their own finalized results: the draft stays with the teacher.
    mine = f"/grading/evaluations/mine?classroom_id={room_id}"
    assert request(u.student, "GET", mine) == []
    request(u.teacher, "GET", mine, 403)
    request(u.other, "GET", mine, 403)
    performance = f"/analytics/performance/{u.student_id}?classroom_id={room_id}"
    assert request(u.teacher, "GET", performance)["skills"] == []

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
        lambda r: len(r["skills"]) == 1,
        "analytics consumption",
    )
    observed = result["skills"][0]
    assert observed["attempted"] == 2 and observed["correct"] == 1 and observed["observed_accuracy"] == 0.5
    assert observed["evidence_status"] == "insufficient_evidence"
    request(u.student, "GET", f"/analytics/performance/{u.teacher_id}?classroom_id={room_id}", 403)
    request(u.other, "GET", performance, 403)
    return Graded(submission["id"], grade["id"], performance, result["skills"])


def summaries(rows: list[dict[str, Any]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The assessment list's rows cut down to entry's fields and `submitted`."""
    return [{k: row[k] for k in [*entry, "submitted"]} for row in rows]


def replayed_events(u: Users, room_id: str, graded: Graded) -> None:
    """Consumers survive a restart, and replayed or re-identified events change nothing."""
    docker("restart", "grading", "analytics")
    replay("assessment", "assessment.submitted.v2", graded.submission_id)
    replay("grading", "grading.finalized.v2", graded.grade_id)
    duplicate_id = replay("grading", "grading.finalized.v2", graded.grade_id, fresh_id=True)
    eventually(
        lambda: sql_value("analytics", f"SELECT count(*) FROM inbox WHERE event_id='{duplicate_id}'"),
        lambda value: value == "1",
        "duplicate processing",
    )
    assert request(u.teacher, "GET", graded.performance_path)["skills"] == graded.skills
    assert len(request(u.teacher, "GET", f"/grading/evaluations?classroom_id={room_id}")) == 1


def class_dashboard(u: Users, room_id: str, graded: Graded) -> None:
    """The class dashboard's numbers, for the classroom's teacher only: submissions per assessment, grades waiting
    and finalized, and one page of enrolled students with their skill evidence."""
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
    assert student["skills"] == graded.skills and student["evidence_status"] == "available"
    request(u.teacher, "GET", f"/analytics/performance{query}&limit=51", 422)
    for path in [progress, counts, performance]:
        request(u.student, "GET", path, 403)
        request(u.other, "GET", path, 403)


@dataclass
class Late:
    assessment_id: str
    skill_id: str
    short: str  # the short-answer question
    free: str  # the free-response question
    answers: dict[str, str]


def lifecycle(u: Users, room_id: str) -> Late:
    """Drafts stay with the teacher until published, and a published assessment cannot change. Students save work,
    and submit after the due date, flagged late."""
    skill = request(
        u.teacher, "POST", "/curriculum/nodes", 201,
        json={"kind": "skill", "name": "Integration photosynthesis", "parent_id": None},
    )  # fmt: skip
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
        {"prompt": "Explain photosynthesis.", "type": "free_response", "rubric": rubric, "max_length": 2000},
    ]
    due = time.time() + 8
    draft = {
        "classroom_id": room_id,
        "title": "Photosynthesis draft",
        "status": "draft",
        "questions": [{**q, "skill_id": skill["id"]} for q in questions],
        "due_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(due)),
        "late_policy": "accept_flagged",
        "release_policy": "manual",
        "ai_marking": True,
    }
    created = request(u.teacher, "POST", "/assessment/assessments", 201, json=draft)
    path = f"/assessment/assessments/{created['id']}"
    assert created["status"] == "draft" and created["questions"][1]["points"] == 5
    listed = f"/assessment/assessments?classroom_id={room_id}"
    # A draft is the teacher's alone: students neither see nor open it.
    assert created["id"] not in [a["id"] for a in request(u.student, "GET", listed)]
    request(u.student, "GET", path, 404)
    request(u.other, "PUT", path, 403, json={**draft, "title": "Forbidden"})
    edited = request(u.teacher, "PUT", path, json={**draft, "title": "Photosynthesis"})
    assert edited["title"] == "Photosynthesis"
    spare = request(u.teacher, "POST", "/assessment/assessments", 201, json={**draft, "title": "Spare draft"})
    request(u.teacher, "DELETE", f"/assessment/assessments/{spare['id']}")
    request(u.teacher, "GET", f"/assessment/assessments/{spare['id']}", 404)

    published = request(u.teacher, "POST", f"{path}/publish")
    assert published["status"] == "published"
    request(u.teacher, "POST", f"{path}/publish", 409)
    request(u.teacher, "PUT", path, 409, json=draft)
    request(u.teacher, "DELETE", path, 409)
    public = request(u.student, "GET", path)
    assert all(k not in q for q in public["questions"] for k in ["answer", "accepted_answers", "rubric"])
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
    return Late(created["id"], skill["id"], short, free, answers)


def marking(u: Users, room_id: str, late: Late) -> None:
    """The teacher marks short answers and rubric questions (with AI suggestions queued, never applied),
    finalizes, and releases results by hand; analytics counts part marks."""
    short, free, answers = late.short, late.free, late.answers

    # Neither answer can be marked automatically: the short answer matches no accepted answer. Each gets an AI
    # suggestion, which waits for ai-core (without provider keys it stays pending with the reason).
    evaluations = f"/grading/evaluations?classroom_id={room_id}&assessment_id={late.assessment_id}"
    [grade] = eventually(lambda: request(u.teacher, "GET", evaluations), lambda rows: len(rows) == 1, "draft")
    assert grade["late"] and grade["answers"] == answers and grade["release_policy"] == "manual"
    assert [e["needs_marking"] for e in grade["evidence"]] == [True, True]
    tried = eventually(
        lambda: request(u.teacher, "GET", evaluations)[0],
        lambda v: all(
            e["suggestion"]["status"] != "pending" or e["suggestion"].get("reason") for e in v["evidence"]
        ),
        "AI marking attempt",
    )
    assert all(e["awarded"] == 0 and "marked_by" not in e for e in tried["evidence"])
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

    # Finalized but not released: the student sees nothing yet.
    mine = f"/grading/evaluations/mine?classroom_id={room_id}"
    assert grade["id"] not in [r["id"] for r in request(u.student, "GET", mine)]
    counts = request(u.teacher, "GET", f"/grading/evaluations/counts?classroom_id={room_id}")
    assert counts == {"drafts": 0, "finalized": 2, "unreleased": 1}
    release = "/grading/evaluations/release"
    request(u.student, "POST", release, 403, json={"assessment_id": late.assessment_id})
    request(u.other, "POST", release, 403, json={"assessment_id": late.assessment_id})
    assert request(u.teacher, "POST", release, json={"assessment_id": late.assessment_id})["released"] == 1
    assert request(u.teacher, "POST", release, json={"assessment_id": late.assessment_id})["released"] == 0
    [result] = [r for r in request(u.student, "GET", mine) if r["id"] == grade["id"]]
    assert [(e["awarded"], e["possible"]) for e in result["evidence"]] == [(2, 2), (3, 5)]
    assert result["evidence"][1]["feedback"] == feedback and result["late"]
    assert all("suggestion" not in e and "criteria" not in e for e in result["evidence"])

    performance = f"/analytics/performance/{u.student_id}?classroom_id={room_id}"
    observed = eventually(
        lambda: request(u.teacher, "GET", performance)["skills"],
        lambda skills: any(s["skill_id"] == late.skill_id for s in skills),
        "part marks in analytics",
    )
    [part] = [s for s in observed if s["skill_id"] == late.skill_id]
    assert (part["attempted"], part["correct"], part["marks_awarded"], part["marks_possible"]) == (2, 1, 5, 7)


def unenroll(u: Users, room_id: str) -> None:
    """Removing a student keeps their grades but ends their access to the classroom."""
    remove = f"/identity/classrooms/{room_id}/enrollments/{u.student_id}"
    request(u.other, "DELETE", remove, 403)
    request(u.student, "DELETE", remove, 403)
    request(u.teacher, "DELETE", remove)
    request(u.teacher, "DELETE", remove, 404)
    assert request(u.teacher, "GET", f"/identity/classrooms/{room_id}/students") == []
    request(u.student, "GET", f"/grading/evaluations/mine?classroom_id={room_id}", 403)
    assert room_id not in [r["id"] for r in request(u.student, "GET", "/identity/classrooms")]
    assert len(request(u.teacher, "GET", f"/grading/evaluations?classroom_id={room_id}")) == 2


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
            lambda service=service, sql=sql: sql_value(service, sql),
            lambda value: value == "0",
            f"{service} purge",
        )
    for service in ["content", "assessment", "grading"]:
        unsent = f"SELECT count(*) FROM outbox WHERE body->'data'->>'classroom_id'='{room}'"
        assert sql_value(service, unsent) == "0", service
    # A repeated deletion and a late submission change nothing.
    duplicate_id = replay("identity", "identity.classroom.deleted.v1", room, fresh_id=True)
    late_id = str(uuid4())
    publish("assessment.submitted.v2", {**late_submission, "event_id": late_id})
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
    for user, database in [
        ("identity", "grading"),
        ("chatbot", "content"),
        ("aicore", "chatbot"),
        ("chatbot", "aicore"),
    ]:
        isolation = psql("SELECT 1", database=database, user=user, check=False)
        assert isolation.returncode != 0 and "permission denied" in isolation.stderr.lower(), (user, database)
    versions = "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations"
    for service, expected in [
        ("content", "1,2"),
        ("identity", "1,2,3,4,5"),
        ("assessment", "1,2"),
        ("grading", "1,2,3"),
        ("analytics", "1,2,3"),
    ]:
        migrations = sql_value(service, versions)
        assert migrations == expected, (service, migrations)
    vector = sql_value("content", "SELECT count(*) FROM pg_extension WHERE extname='vector'")
    assert vector == "1", "pgvector is not installed in Content's database"


def ai_service_accounts(u: Users) -> None:
    """Service accounts reach the private AI service (200 with provider keys, 503 without); people do not."""
    for client_id in ["content-indexer", "question-chatbot-agent", "grading-marker"]:
        assert ai_core_status(service_token(client_id)) in (200, 503)
    assert ai_core_status(u.student.headers["Authorization"].removeprefix("Bearer ")) == 403


def rate_limit_per_session() -> None:
    """Kong's rate limit is each signed-in session's own: one session using it up leaves others unaffected."""
    busy, other = login("teacher", "TEACHER_PASSWORD"), login("student", "STUDENT_PASSWORD")
    statuses = [busy.get("/identity/classrooms").status_code for _ in range(125)]
    assert statuses.count(429) > 0, "Kong did not rate limit a session making 125 requests in a minute"
    request(other, "GET", "/identity/classrooms")


def main() -> None:
    users = sign_in()
    room_id = classroom(users)["id"]
    documents(users, room_id)
    search(users, room_id)
    chat(users, room_id)
    graded = assess_and_grade(users, room_id)
    replayed_events(users, room_id, graded)
    class_dashboard(users, room_id, graded)
    marking(users, room_id, lifecycle(users, room_id))
    unenroll(users, room_id)
    delete_classroom(users, room_id, graded)
    databases()
    ai_service_accounts(users)
    rate_limit_per_session()
    print(
        "PASS: gateway auth, classroom isolation, enrollment by username, rosters, the school's student list, unenrollment, classroom deletion and its cross-service purge, curriculum, "
        "S3 upload/extraction, PDF indexing jobs, teacher-only rubrics, search access, chat threads and streaming, "
        "AI spend allowance, answer-key secrecy, submissions, draft grading, teacher finalization, students' own "
        "finalized results, the class dashboard's counts and class performance, Kafka analytics, consumer restart, duplicate event and business-key idempotency, "
        "draft and scheduled assessments, saved answers, late submissions, short-answer and rubric marking, "
        "AI marking suggestions, manual release, part marks in analytics, "
        "student privacy, database isolation, numbered migrations, pgvector, AI service accounts, per-session rate limiting."
    )


if __name__ == "__main__":
    main()
