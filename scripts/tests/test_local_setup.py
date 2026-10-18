"""The local stack's setup scripts, Compose file and Kong configuration agree with each other. None of these tests
needs Docker or the running stack."""

import json
import re
import subprocess
from pathlib import Path

import bootstrap
import local_stack
import pytest
import upgrade_local
import wait_ready
from setup import generate_secrets

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def required_variables() -> set[str]:
    """Every ${NAME:?message} in docker-compose.yml: Compose refuses to start without it."""
    return set(re.findall(r"\$\{(\w+):\?", COMPOSE))


def test_every_value_compose_requires_is_generated_and_in_the_example() -> None:
    required = required_variables()
    assert {"GRADING_MARKER_CLIENT_SECRET", "KEYCLOAK_ADMIN_PASSWORD", "S3_CONTENT_PASSWORD"} <= required
    assert required <= generate_secrets().keys()
    example = local_stack.read_env(ROOT / ".env.example")
    assert required <= example.keys(), required - example.keys()


def test_no_unguarded_secret_in_compose() -> None:
    """A plain ${NAME} would silently become empty; only optional values carry a default."""
    assert re.findall(r"\$\{\w+\}", COMPOSE) == []
    optional = set(re.findall(r"\$\{(\w+):-", COMPOSE))
    assert optional == {
        "OPENAI_API_KEY",
        "VOYAGE_API_KEY",
        "ANALYTICS_KAFKA_GROUP",
        *("KONG_PORT", "KEYCLOAK_PORT", "WEB_PORT"),  # the published ports, which the test stack moves
    }


def test_upgrade_adds_only_what_env_lacks(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# old setup\nPOSTGRES_PASSWORD=kept\nCHATBOT_DB_PASSWORD=\n", encoding="utf-8")
    env = upgrade_local.add_missing_secrets(env_path)
    assert env["POSTGRES_PASSWORD"] == "kept"
    assert env["GRADING_MARKER_CLIENT_SECRET"] and env["CHATBOT_DB_PASSWORD"]
    assert env["S3_CONTENT_USER"] == "content-service"
    assert required_variables() <= env.keys()
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# old setup", "POSTGRES_PASSWORD=kept"]
    assert sum(line.startswith("POSTGRES_PASSWORD=") for line in lines) == 1
    # Running it again adds nothing.
    before = env_path.read_text(encoding="utf-8")
    upgrade_local.add_missing_secrets(env_path)
    assert env_path.read_text(encoding="utf-8") == before


def test_kong_waits_longer_than_contents_upload_deadline() -> None:
    domain = (ROOT / "services" / "content" / "domain.go").read_text(encoding="utf-8")
    deadline = re.search(r"uploadDeadline\s*=\s*(\d+) \* time.Minute", domain)
    assert deadline, "Content's uploadDeadline moved; update this test and infra/kong/bootstrap.py"
    minutes = int(deadline.group(1))
    content = bootstrap.business_service("content")
    assert content["read_timeout"] > minutes * 60_000 and content["write_timeout"] > minutes * 60_000
    assert bootstrap.business_service("identity")["read_timeout"] == 20_000


def probe_result(stdout: str, code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, "")


@pytest.mark.parametrize(
    ("outcome", "ready"),
    [
        (subprocess.TimeoutExpired("docker", 40), False),
        (probe_result("Error: service analytics is not running"), False),
        (probe_result("", 1), False),
        (probe_result(json.dumps(dict.fromkeys(wait_ready.SERVICES, 200))), True),
        (probe_result(json.dumps({**dict.fromkeys(wait_ready.SERVICES, 200), "grading": 503})), False),
    ],
)
def test_readiness_probe_retries_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, outcome: object, ready: bool
) -> None:
    def run(*_args: object, **_kwargs: object) -> object:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(wait_ready.subprocess, "run", run)
    assert wait_ready.applications_ready() is ready


def test_psql_json_keeps_tabs_and_line_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def value(sql: str, **_kwargs: str) -> str:
        seen.append(sql)
        return json.dumps([{"title": "Week 1\tintro\nPart 2", "ids": ["a", "b"], "tokens": None}])

    monkeypatch.setattr(local_stack, "psql_value", value)
    rows = local_stack.psql_json("SELECT title FROM documents")
    assert rows == [{"title": "Week 1\tintro\nPart 2", "ids": ["a", "b"], "tokens": None}]
    assert seen == ["SELECT coalesce(json_agg(r), '[]') FROM (SELECT title FROM documents) r"]
