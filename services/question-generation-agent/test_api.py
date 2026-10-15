import pytest
from fastapi.testclient import TestClient
from learning_transport.testing import as_caller, token_claims

from main import app

PATH = "/v1/question_generation"
PAYLOAD = {
    "document_ids": ["00000000-0000-4000-8000-000000000001"],
    "skill_ids": ["00000000-0000-4000-8000-000000000002"],
    "classroom_id": "00000000-0000-4000-8000-000000000003",
    "question_type": "numerical",
    "difficulty": "introductory",
    "count": 2,
}


def test_placeholder_contract():
    client = TestClient(app)
    # Explicit test double for verified OIDC claims, never wired in the application.
    with as_caller(app, token_claims("ai-client", sub="test-service")):
        result = client.post(PATH, json=PAYLOAD)
        assert result.status_code == 501
        assert result.json()["error"]["code"] == "AI_CAPABILITY_NOT_IMPLEMENTED"
        assert result.json()["error"]["capability"] == "question_generation"
        assert client.post(PATH, json={}).status_code == 422
        assert client.post(PATH, json={**PAYLOAD, "unknown": True}).status_code == 422


@pytest.mark.parametrize("role", ["student", "teacher"])
def test_no_anonymous_or_people_access(role):
    client = TestClient(app)
    assert client.post(PATH, json=PAYLOAD).status_code == 401
    with as_caller(app, token_claims(role)):
        assert client.post(PATH, json=PAYLOAD).status_code == 403


def test_ready_reports_the_capability_is_not_implemented():
    assert TestClient(app).get("/health/ready").json() == {"status": "ready", "capability": "not_implemented"}
