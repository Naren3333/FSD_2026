"""fakes for the agent's tests: identity, analytics, grading, assessment, curriculum, ai-core and keycloak's token endpoint behind one httpx mocktransport."""

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
STUDENT = "student-1"
LOOPS = "00000000-0000-4000-8000-0000000000a1"
VARIABLES = "00000000-0000-4000-8000-0000000000a2"
QUIZ = "00000000-0000-4000-8000-0000000000b1"
EVALUATION = "00000000-0000-4000-8000-0000000000e1"
Q1 = "00000000-0000-4000-8000-0000000000c1"
Q2 = "00000000-0000-4000-8000-0000000000c2"
Q3 = "00000000-0000-4000-8000-0000000000c3"
SETTINGS = Settings(
    identity_url="http://identity:8080",
    content_url="",
    ai_core_url="http://ai-core:8080",
    token_url="http://keycloak:8080/token",
    client_id="student-performance-agent",
    client_secret="secret",
    analytics_url="http://analytics:8080",
    grading_url="http://grading:8080",
    assessment_url="http://assessment:8080",
    curriculum_url="http://curriculum:8080",
)


def reply(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, json={"output": json.dumps(body), "model_version": "gpt-test", "usage_tokens": 9}
    )


def report(**changes: Any) -> dict[str, Any]:
    """a report citing the quiz's lost loop question, as a final turn returns it."""
    return {
        "observations": [{"text": "Loops are where marks were lost.", "evidence": ["E1", "E2"]}],
        "possible_misconceptions": [{"text": "Thinks range(3) includes 3.", "evidence": ["E1"]}],
        "suggested_interventions": ["Trace a short loop on the board together."],
        "recommended_topics": ["T1"],
        **changes,
    }


def done(**changes: Any) -> dict[str, Any]:
    """a turn that reports before the last turn."""
    return {"action": "report", "tool_calls": [], "report": report(**changes)}


def tools(*calls: dict[str, Any]) -> dict[str, Any]:
    return {"action": "call_tools", "tool_calls": list(calls), "report": None}


def call(tool: str, result: str | None = None, topic: str | None = None) -> dict[str, Any]:
    return {"tool": tool, "result": result, "topic": topic, "reason": "to check"}


def finalized(**changes: Any) -> dict[str, Any]:
    return {
        "id": EVALUATION,
        "assessment_id": QUIZ,
        "classroom_id": ROOM,
        "student_id": STUDENT,
        "status": "finalized",
        "evidence": [
            {
                "question_id": Q1,
                "topic_id": LOOPS,
                "correct": False,
                "awarded": 0,
                "possible": 2,
                "feedback": "Off by one.",
                "suggestion": {"status": "ready", "feedback": "AI words"},
            },
            {"question_id": Q2, "topic_id": LOOPS, "correct": True, "awarded": 2, "possible": 2},
            {"question_id": Q3, "topic_id": VARIABLES, "correct": True, "awarded": 1, "possible": 1},
        ],
        "answers": {Q1: "0, 1, 2, 3", Q2: "4", Q3: "x = 5"},
        "late": False,
        "finalized_at": "2026-10-20T10:00:00Z",
        **changes,
    }


QUIZ_ASSESSMENT = {
    "id": QUIZ,
    "classroom_id": ROOM,
    "title": "Loops quiz",
    "questions": [
        {
            "id": Q1,
            "prompt": "What does range(3) produce?",
            "type": "short_answer",
            "answer": "",
            "accepted_answers": ["0, 1, 2"],
            "points": 2,
            "topic_id": LOOPS,
        },
        {
            "id": Q2,
            "prompt": "How many times does the loop run?",
            "type": "numerical",
            "answer": "4",
            "points": 2,
            "topic_id": LOOPS,
        },
        {
            "id": Q3,
            "prompt": "Assign 5 to x.",
            "type": "short_answer",
            "answer": "",
            "accepted_answers": ["x = 5"],
            "points": 1,
            "topic_id": VARIABLES,
        },
    ],
}


class Fakes:
    """what the other services answer."""

    def __init__(self) -> None:
        self.teachers = {("Bearer t1", ROOM)}
        self.evaluations: list[dict[str, Any]] = [finalized(), finalized(id="draft", status="draft")]
        self.assessments = {QUIZ: QUIZ_ASSESSMENT}
        self.topics = {LOOPS: "Loops", VARIABLES: "Variables"}
        self.summary = [
            {"topic_id": LOOPS, "attempted": 2, "correct": 1, "observed_accuracy": 0.5, "marks_awarded": 2,
             "marks_possible": 4, "evidence_status": "insufficient_evidence", "weakness_indicator": False},
        ]  # fmt: skip
        self.prerequisites = {LOOPS: [{"id": VARIABLES, "name": "Variables"}]}
        self.status: dict[str, int] = {}  # a service's path prefix → the status it fails with
        self.replies: list[httpx.Response] = []
        self.requests: list[tuple[str, dict[str, Any], httpx.Request]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        is_json = request.headers.get("content-type") == "application/json"
        body = json.loads(request.content) if request.method == "POST" and is_json else {}
        self.requests.append((path, body, request))
        token = request.headers.get("authorization")
        for prefix, status in self.status.items():
            if path.startswith(prefix):
                return httpx.Response(status, json={"error": {"code": "X", "message": "x"}})
        if path == "/token":
            return httpx.Response(200, json={"access_token": "service-token", "expires_in": 300})
        if path.endswith("/access"):
            classroom = path.split("/")[3]
            enrolled = request.url.params.get("student_id") == STUDENT
            if (token, classroom) in self.teachers and enrolled:
                return httpx.Response(200, json={"allowed": True, "relation": "teacher"})
            return httpx.Response(403, json={"error": {"code": "FORBIDDEN", "message": "denied"}})
        if path == "/v1/inference":
            assert token == "Bearer service-token", "ai-core must get the agent's token"
            return self.replies.pop(0)
        return self.business(path, body, request)

    def business(self, path: str, body: dict[str, Any], request: httpx.Request) -> httpx.Response:
        """what analytics, grading, assessment and curriculum answer."""
        if path == f"/v1/performance/{STUDENT}":
            return httpx.Response(200, json={"student_id": STUDENT, "classroom_id": ROOM, "topics": self.summary,
                                             "evidence_status": "available", "interpretation": "x"})  # fmt: skip
        if path == "/v1/performance":
            other = {"student_id": "student-2", "topics": [{**self.summary[0], "attempted": 4, "correct": 4}]}
            return httpx.Response(200, json={"classroom_id": ROOM, "total": 2, "offset": 0, "interpretation": "x",
                                             "students": [{"student_id": STUDENT, "topics": self.summary}, other]})  # fmt: skip
        if path == "/v1/evaluations":
            assert request.url.params["student_id"] == STUDENT
            return httpx.Response(200, json={"evaluations": self.evaluations, "total": len(self.evaluations)})
        if path == "/v1/assessments/batch":
            if any(i not in self.assessments for i in body["ids"]):
                return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})
            return httpx.Response(200, json=[self.assessments[i] for i in body["ids"]])
        if path.startswith("/v1/assessments/"):
            found = self.assessments.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if path.startswith("/v1/topics/") and path.endswith("/prerequisites"):
            return httpx.Response(200, json=self.prerequisites.get(path.split("/")[3], []))
        if path.startswith("/v1/topics/"):
            topic = path.rsplit("/", 1)[1]
            if topic not in self.topics:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": topic, "name": self.topics[topic]})
        return httpx.Response(404)

    def calls(self, path: str) -> list[dict[str, Any]]:
        return [body for p, body, _ in self.requests if p == path]

    def headers(self, path: str) -> list[httpx.Headers]:
        return [r.headers for p, _, r in self.requests if p == path]

    def task(self, turn: int) -> dict[str, Any]:
        """the task document the model was given on a turn, from 1."""
        return dict(json.loads(self.calls("/v1/inference")[turn - 1]["messages"][1]["content"]))


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
