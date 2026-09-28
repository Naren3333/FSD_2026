from fastapi.testclient import TestClient
from learning_transport import actor
from main import app

PAYLOAD = {
    "student_id": "student",
    "classroom_id": "00000000-0000-4000-8000-000000000003",
    "document_ids": ["00000000-0000-4000-8000-000000000001"],
    "question": "How do fractions add?",
    "conversation_id": "00000000-0000-4000-8000-000000000004",
    "retrieved_evidence": [],
}


def test_placeholder_contract():
    # Explicit test double for verified OIDC claims, never wired in the application.
    app.dependency_overrides[actor] = lambda: {
        "sub": "test-service",
        "org_id": "test-org",
        "realm_access": {"roles": ["ai-client"]},
    }
    try:
        client = TestClient(app)
        result = client.post("/v1/grounded_question_answering", json=PAYLOAD)
        assert result.status_code == 501
        assert result.json()["error"]["code"] == "AI_CAPABILITY_NOT_IMPLEMENTED"
        assert result.json()["error"]["capability"] == "grounded_question_answering"
        assert client.post("/v1/grounded_question_answering", json={}).status_code == 422
        assert (
            client.post("/v1/grounded_question_answering", json={**PAYLOAD, "unknown": True}).status_code
            == 422
        )
    finally:
        app.dependency_overrides.clear()


def test_no_anonymous_or_student_access():
    client = TestClient(app)
    assert client.post("/v1/grounded_question_answering", json=PAYLOAD).status_code == 401
    app.dependency_overrides[actor] = lambda: {"realm_access": {"roles": ["student"]}}
    try:
        assert client.post("/v1/grounded_question_answering", json=PAYLOAD).status_code == 403
    finally:
        app.dependency_overrides.clear()
