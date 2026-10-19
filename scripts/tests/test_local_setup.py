"""The local stack's setup scripts, Compose file and Kong configuration agree with each other. None of these tests
needs Docker or the running stack."""

import json
import re
import subprocess
from pathlib import Path

import bootstrap
import httpx
import local_stack
import pytest
import setup
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
        "LOCAL_CONFIG_DIR",  # the test stack's own first-start files
        *("GO_DB_POOL_MAX", "PYTHON_DB_POOL_MAX", "DB_POOL_MIN"),
        *("SELECTIVE_VISION", "CHUNK_TOKENS", "CHUNK_OVERLAP", "SLIDE_CHUNKS"),
        *("ADAPTIVE_REWRITE", "ADAPTIVE_SEARCH", "CHAT_CONTEXT_TOKENS"),
    }


def test_read_env_reads_values_as_compose_does(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        '# comment\n\n  KONG_PORT = 9000 \nexport TEACHER_PASSWORD=a=b\nQUOTED="x y"\nSINGLE=\'z\'\nODD="q\n'
        "EMPTY=\nnot a pair\n",
        encoding="utf-8",
    )
    assert local_stack.read_env(path) == {
        "KONG_PORT": "9000",
        "TEACHER_PASSWORD": "a=b",
        "QUOTED": "x y",
        "SINGLE": "z",
        "ODD": '"q',
        "EMPTY": "",
    }


def test_every_application_has_a_healthcheck() -> None:
    """The Go images are distroless, so their binary checks itself; the Python images ask with Python."""
    go = {"identity", "curriculum", "content", "assessment", "grading"}
    for name in wait_ready.SERVICES:
        block = re.search(
            rf"^  {re.escape(name)}:\n((?:    .*\n|      .*\n|        .*\n)*)", COMPOSE, re.MULTILINE
        )
        assert block, name
        expected = "*go-healthcheck" if name in go else "*python-healthcheck"
        assert f"    healthcheck: {expected}\n" in block[1], name
    assert "  - -healthcheck\n" in COMPOSE
    platform = (ROOT / "packages/platform/health.go").read_text(encoding="utf-8")
    assert 'HealthcheckFlag = "-healthcheck"' in platform


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


def test_people_leave_the_attribute_policy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_person turns on admin-editable unmanaged attributes once and never back off, so people created by two
    scripts at once both keep their org_id."""
    profiles: list[dict[str, object]] = []
    profile: dict[str, object] = {"attributes": []}

    def handle(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/users/profile":
            if method == "PUT":
                profiles.append(json.loads(request.content))
                profile.update(profiles[-1])
            return httpx.Response(200 if method == "GET" else 204, json=profile if method == "GET" else None)
        if path == "/users" and method == "GET":
            return httpx.Response(200, json=[{"id": "u1"}])
        if path.startswith("/roles/"):
            return httpx.Response(200, json={"name": "student"})
        if path == "/users/u1" and method == "GET":
            return httpx.Response(200, json={"id": "u1", "attributes": {}})
        return httpx.Response(204)

    admin = httpx.Client(base_url="http://keycloak.test", transport=httpx.MockTransport(handle))
    for _ in range(2):
        local_stack.ensure_person(admin, "s", "S", "Local", "pw")
    assert [p["unmanagedAttributePolicy"] for p in profiles] == ["ADMIN_EDIT"]


def test_upgrade_updates_the_web_client() -> None:
    old = {
        "id": "w",
        "clientId": "learning-web",
        "redirectUris": [f"{setup.WEB_ORIGIN}/*", f"{setup.TEST_WEB_ORIGIN}/*"],
        "webOrigins": [setup.WEB_ORIGIN, setup.TEST_WEB_ORIGIN],
        "attributes": {"pkce.code.challenge.method": "S256", "kept": "1"},
    }
    updated = upgrade_local.updated_web_client(old, development=True)
    assert updated["redirectUris"] == [f"{setup.WEB_ORIGIN}/*"]
    assert updated["webOrigins"] == [setup.WEB_ORIGIN]
    assert updated["attributes"] == {**setup.WEB_ATTRIBUTES, "kept": "1"}
    # Running again changes nothing.
    assert upgrade_local.updated_web_client(updated, development=True) == updated
    # The test stack's own realm keeps its origin.
    assert upgrade_local.updated_web_client(old, development=False)["webOrigins"] == old["webOrigins"]


class FakeKeycloak:
    """The parts of the learning realm's admin API that upgrade_local uses: clients by clientId, their service
    accounts, and role mappings (which Keycloak keeps as a set, so a repeated mapping changes nothing)."""

    def __init__(self, clients: set[str]) -> None:
        self.clients = {name: f"id-{name}" for name in {*clients, setup.ACCOUNTS_ROLE[0]}}
        self.created: list[str] = []
        self.mappings: set[tuple[str, str]] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/clients" and method == "GET":
            name = request.url.params["clientId"]
            found = [{"id": self.clients[name], "clientId": name}] if name in self.clients else []
            return httpx.Response(200, json=found)
        if path == "/clients" and method == "POST":
            name = json.loads(request.content)["clientId"]
            self.clients[name] = f"id-{name}"
            self.created.append(name)
            return httpx.Response(201)
        if path.endswith("/service-account-user"):
            return httpx.Response(200, json={"id": "account-" + path.split("/")[2]})
        if method == "GET" and "/roles/" in path:
            return httpx.Response(200, json={"name": path.rsplit("/", 1)[1]})
        if method == "POST" and "/role-mappings/" in path:
            for role in json.loads(request.content):
                self.mappings.add((path.split("/")[2], role["name"]))
            return httpx.Response(204)
        return httpx.Response(404, text=f"unexpected {method} {path}")

    def admin(self) -> httpx.Client:
        return httpx.Client(base_url="http://keycloak.test", transport=httpx.MockTransport(self.handle))


def test_upgrade_assigns_the_accounts_role_after_a_half_finished_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """An earlier run created identity-accounts and stopped before giving its service account manage-users: the
    next run assigns it without creating the client again."""
    keycloak = FakeKeycloak({setup.ACCOUNTS_CLIENT})
    monkeypatch.setattr(upgrade_local, "admin_client", keycloak.admin)
    upgrade_local.add_missing_accounts_client(generate_secrets())
    assert keycloak.created == []
    assert keycloak.mappings == {(f"account-id-{setup.ACCOUNTS_CLIENT}", setup.ACCOUNTS_ROLE[1])}
    # A fresh Keycloak gets the client and the role; running again changes nothing.
    keycloak = FakeKeycloak(set())
    monkeypatch.setattr(upgrade_local, "admin_client", keycloak.admin)
    upgrade_local.add_missing_accounts_client(generate_secrets())
    upgrade_local.add_missing_accounts_client(generate_secrets())
    assert keycloak.created == [setup.ACCOUNTS_CLIENT]
    assert keycloak.mappings == {(f"account-id-{setup.ACCOUNTS_CLIENT}", setup.ACCOUNTS_ROLE[1])}


def test_upgrade_assigns_the_ai_client_role_to_existing_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    keycloak = FakeKeycloak({setup.AI_CLIENTS[0]})
    monkeypatch.setattr(upgrade_local, "admin_client", keycloak.admin)
    upgrade_local.add_missing_clients(generate_secrets())
    assert keycloak.created == setup.AI_CLIENTS[1:]
    assert keycloak.mappings == {(f"account-id-{client}", "ai-client") for client in setup.AI_CLIENTS}
