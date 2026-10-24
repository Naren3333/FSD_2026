import pytest

import config

ENV = {
    "IDENTITY_URL": "http://identity",
    "CONTENT_URL": "http://content",
    "AI_CORE_URL": "http://ai-core",
    "AI_TOKEN_URL": "http://keycloak/token",
    "AI_CLIENT_ID": "question-generation-agent",
    "AI_CLIENT_SECRET": "secret",
    "CURRICULUM_URL": "http://curriculum",
}


@pytest.fixture(autouse=True)
def required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("QUESTION_REVIEW", "QUESTION_REVIEW_RETRIES", "QUESTION_REVIEW_DEADLINE"):
        monkeypatch.delenv(key, raising=False)


def test_review_defaults():
    settings = config.load()
    assert (settings.review, settings.review_retries, settings.review_deadline) == (True, 2, 240.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("off", False),
        ("OFF", False),
        (" false ", False),
        ("0", False),
        ("no", False),
        ("on", True),
        ("maybe", True),
    ],
)
def test_review_switch(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool):
    monkeypatch.setenv("QUESTION_REVIEW", value)
    assert config.load().review is expected


def test_retries_are_clamped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QUESTION_REVIEW_RETRIES", "9")
    assert config.load().review_retries == 2
    monkeypatch.setenv("QUESTION_REVIEW_RETRIES", "-1")
    assert config.load().review_retries == 0


@pytest.mark.parametrize(("value", "expected"), [("999", 240.0), ("5", 30.0), ("120", 120.0)])
def test_deadline_is_clamped(monkeypatch: pytest.MonkeyPatch, value: str, expected: float):
    monkeypatch.setenv("QUESTION_REVIEW_DEADLINE", value)
    assert config.load().review_deadline == expected
