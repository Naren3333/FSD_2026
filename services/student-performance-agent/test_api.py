import pytest
from fastapi.testclient import TestClient
from learning_transport.testing import as_caller, token_claims

from main import app

PATH = "/v1/performance_interpretation"
PAYLOAD = {
    "student_id": "student",
    "classroom_id": "00000000-0000-4000-8000-000000000003",
    "performance_summary": [
        {"topic_id": "00000000-0000-4000-8000-000000000002", "attempted": 2, "correct": 1}
    ],
    "evidence_ids": ["00000000-0000-4000-8000-000000000001"],
    "topic_ids": ["00000000-0000-4000-8000-000000000002"],
}


def test_placeholder_contract():
    client = TestClient(app)
    # explicit test double for verified oidc claims, never wired in the application.
    with as_caller(app, token_claims("ai-client", sub="test-service")):
        result = client.post(PATH, json=PAYLOAD)
        assert result.status_code == 501
        assert result.json()["error"]["code"] == "AI_CAPABILITY_NOT_IMPLEMENTED"
        assert result.json()["error"]["capability"] == "performance_interpretation"
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
