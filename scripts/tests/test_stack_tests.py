"""verify test-stack isolation, port selection and first-start configuration."""

import importlib
import json
import os
import re
import subprocess
from pathlib import Path

import local_stack
import pytest
import scale_demo
import setup
import stack_tests
import wait_ready
import yaml

ROOT = Path(__file__).resolve().parents[2]


def compose_result(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def test_project_names_match_compose_and_the_browser_tests() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^name: (\S+)$", compose, re.MULTILINE)[1] == local_stack.DEV_PROJECT  # type: ignore[index]
    playwright = (ROOT / "apps/web/playwright.config.ts").read_text(encoding="utf-8")
    assert f"process.env['COMPOSE_PROJECT_NAME'] !== '{local_stack.TEST_PROJECT}'" in playwright
    assert local_stack.TEST_PROJECT != local_stack.DEV_PROJECT


def test_the_browser_tests_require_the_test_ports() -> None:
    """the browser tests' guard names the same test ports as local_stack, so the project name alone is not enough."""
    playwright = (ROOT / "apps/web/playwright.config.ts").read_text(encoding="utf-8")
    ports = re.search(r"const TEST_PORTS: Record<string, string> = \{(.*)\};", playwright)
    assert ports, "playwright.config.ts lost its TEST_PORTS"
    assert dict(re.findall(r"(\w+): '(\d+)'", ports[1])) == local_stack.TEST_PORTS
    assert "process.env[name] !== TEST_PORTS[name]" in playwright


def test_data_writing_scripts_refuse_the_development_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    with pytest.raises(SystemExit, match="stack_tests.py"):
        local_stack.require_test_stack(dev=False)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", local_stack.DEV_PROJECT)
    with pytest.raises(SystemExit, match="--dev"):
        local_stack.require_test_stack(dev=False)
    local_stack.require_test_stack(dev=True)  # --dev: no check, no docker call


def test_data_writing_scripts_refuse_the_development_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """the test project name with the development ports would send psql to the test stack but http to the development stack's kong and keycloak."""
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", local_stack.TEST_PROJECT)
    monkeypatch.setattr(local_stack.subprocess, "run", lambda *_a, **_k: pytest.fail("checked docker first"))
    monkeypatch.setattr(local_stack, "KONG_PORT", "8000")
    monkeypatch.setattr(local_stack, "KEYCLOAK_PORT", local_stack.TEST_PORTS["KEYCLOAK_PORT"])
    with pytest.raises(SystemExit, match="KONG_PORT=8000"):
        local_stack.require_test_stack(dev=False)
    monkeypatch.setattr(local_stack, "KONG_PORT", local_stack.TEST_PORTS["KONG_PORT"])
    monkeypatch.setattr(local_stack, "KEYCLOAK_PORT", "8081")
    with pytest.raises(SystemExit, match="KEYCLOAK_PORT=8081"):
        local_stack.require_test_stack(dev=False)


def test_data_writing_scripts_need_the_test_stack_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", local_stack.TEST_PROJECT)
    monkeypatch.setattr(local_stack, "KONG_PORT", local_stack.TEST_PORTS["KONG_PORT"])
    monkeypatch.setattr(local_stack, "KEYCLOAK_PORT", local_stack.TEST_PORTS["KEYCLOAK_PORT"])
    outputs = iter(["", "4f2a9c\n"])
    monkeypatch.setattr(local_stack.subprocess, "run", lambda *_a, **_k: compose_result(next(outputs)))
    with pytest.raises(SystemExit, match="not running"):
        local_stack.require_test_stack(dev=False)
    local_stack.require_test_stack(dev=False)


def test_the_test_stack_publishes_its_own_ports() -> None:
    """every published port and every url the stack hands out follows the port variables, and the test stack's differ from the development stack's defaults, so both run at once."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    published = re.findall(r"^    - 127\.0\.0\.1:(\S+):\d+$", compose, re.MULTILINE)
    assert published == ["${KEYCLOAK_PORT:-8081}", "${KONG_PORT:-8000}", "${WEB_PORT:-4200}"]
    assert re.findall(r"http://localhost:(\d+)", compose) == []
    defaults = {"KONG_PORT": "8000", "KEYCLOAK_PORT": "8081", "WEB_PORT": "4200"}
    assert local_stack.TEST_PORTS.keys() == defaults.keys()
    assert set(local_stack.TEST_PORTS.values()).isdisjoint(defaults.values())
    assert f"http://localhost:{local_stack.TEST_PORTS['WEB_PORT']}" == setup.TEST_WEB_ORIGIN


def test_each_stack_realm_allows_only_its_own_web_origin() -> None:
    values = setup.generate_secrets()
    for web_origin, realm in [
        (setup.WEB_ORIGIN, setup.realm(values)),
        (setup.TEST_WEB_ORIGIN, setup.realm(values, setup.TEST_WEB_ORIGIN)),
    ]:
        web = realm["clients"][0]
        assert web["clientId"] == "learning-web"
        assert web["webOrigins"] == [web_origin]
        assert web["redirectUris"] == [f"{web_origin}/*"]
        # sign out returns to a sign-in redirect uri.
        assert web["attributes"]["post.logout.redirect.uris"] == "+"


def test_scripts_follow_the_port_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, port in local_stack.TEST_PORTS.items():
        monkeypatch.setenv(name, port)
    stack = importlib.reload(local_stack)
    try:
        assert stack.BASE == "http://127.0.0.1:18000/api"
        assert stack.TOKEN_URL.startswith("http://127.0.0.1:18081/realms/learning/")
        ready = importlib.reload(wait_ready)
        assert [url for url, _ in ready.PUBLIC.values()] == [
            "http://127.0.0.1:18000/api/identity/classrooms",
            "http://127.0.0.1:18081/realms/learning",
        ]
    finally:
        monkeypatch.undo()
        importlib.reload(local_stack)
        importlib.reload(wait_ready)


def test_scripts_read_ports_from_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """as compose does: the environment first, then .env, then the development default."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("KONG_PORT=9000\n", encoding="utf-8")
    read = local_stack.read_env
    monkeypatch.setattr(local_stack, "read_env", lambda: read(dotenv))
    monkeypatch.delenv("KONG_PORT", raising=False)
    monkeypatch.delenv("KEYCLOAK_PORT", raising=False)
    assert local_stack.published_port("KONG_PORT", "8000") == "9000"
    assert local_stack.published_port("KEYCLOAK_PORT", "8081") == "8081"
    monkeypatch.setenv("KONG_PORT", "18000")
    assert local_stack.published_port("KONG_PORT", "8000") == "18000"
    dotenv.unlink()
    monkeypatch.delenv("KONG_PORT")
    assert local_stack.published_port("KONG_PORT", "8000") == "8000"


def test_fresh_volumes_get_every_database_and_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """the test stack's first-start files go to their own directory, which docker-compose.yml mounts for it, so the development stack's realm import never names the test origin."""
    values = setup.generate_secrets()
    monkeypatch.setattr(stack_tests, "ROOT", tmp_path)
    monkeypatch.setattr(stack_tests, "env", lambda: values)
    stack_tests.refresh_local_files()
    local = tmp_path / stack_tests.TEST_LOCAL_CONFIG_DIR
    sql = (local / "postgres-init.sql").read_text(encoding="utf-8")
    assert all(f"CREATE DATABASE {name} OWNER {name};" in sql for name in setup.SERVICES)
    realm = json.loads((local / "learning-realm.json").read_text(encoding="utf-8"))
    assert realm == setup.realm(values, setup.TEST_WEB_ORIGIN)
    assert not (tmp_path / ".local/learning-realm.json").exists()
    assert stack_tests.TEST_ENV["LOCAL_CONFIG_DIR"] == f"./{stack_tests.TEST_LOCAL_CONFIG_DIR}"
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${LOCAL_CONFIG_DIR:-./.local}/postgres-init.sql:" in compose
    assert "${LOCAL_CONFIG_DIR:-./.local}/learning-realm.json:" in compose


def test_an_old_env_is_sent_to_upgrade_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = setup.generate_secrets()
    del values["AICORE_DB_PASSWORD"]
    monkeypatch.setattr(stack_tests, "ROOT", tmp_path)
    monkeypatch.setattr(stack_tests, "env", lambda: values)
    with pytest.raises(SystemExit, match="upgrade_local"):
        stack_tests.refresh_local_files()
    assert not (tmp_path / ".local").exists()


def test_rejects_an_unknown_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["stack_tests.py", "integration", "unit"])
    with pytest.raises(SystemExit) as stopped:
        stack_tests.main()
    assert stopped.value.code == 2


def test_the_scale_demo_runs_only_when_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """a run without suites leaves the load test out; naming it adds its compose file after any already set."""
    assert "scale" in stack_tests.SUITES and "scale" not in stack_tests.DEFAULT_SUITES
    monkeypatch.setitem(stack_tests.TEST_ENV, "COMPOSE_FILE", "docker-compose.yml")
    stack_tests.with_scale_file()
    assert stack_tests.TEST_ENV["COMPOSE_FILE"].split(os.pathsep) == [
        "docker-compose.yml",
        "docker-compose.scale.yml",
    ]


def test_the_scaled_services_can_run_as_replicas() -> None:
    """docker-compose.scale.yml replicates the services scale_demo.py measures, and none of them fixes a container name or a host port, which only one replica could hold."""
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    scaled = yaml.safe_load((ROOT / "docker-compose.scale.yml").read_text(encoding="utf-8"))["services"]
    replicated = {name for name, service in scaled.items() if "replicas" in service.get("deploy", {})}
    assert replicated == set(scale_demo.SCALED)
    for name in replicated:
        assert "container_name" not in base[name] and "ports" not in base[name], name
