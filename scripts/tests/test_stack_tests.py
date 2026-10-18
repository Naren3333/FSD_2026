"""The disposable test stack: the scripts that create and delete data refuse the development stack unless told, the
test stack publishes its own ports, and scripts/stack_tests.py writes its volumes' first-start files. None of these
tests needs Docker."""

import importlib
import json
import re
import subprocess
from pathlib import Path

import local_stack
import pytest
import setup
import stack_tests
import wait_ready

ROOT = Path(__file__).resolve().parents[2]


def compose_result(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def test_project_names_match_compose_and_the_browser_tests() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^name: (\S+)$", compose, re.MULTILINE)[1] == local_stack.DEV_PROJECT  # type: ignore[index]
    playwright = (ROOT / "apps/web/playwright.config.ts").read_text(encoding="utf-8")
    assert f"process.env['COMPOSE_PROJECT_NAME'] !== '{local_stack.TEST_PROJECT}'" in playwright
    assert local_stack.TEST_PROJECT != local_stack.DEV_PROJECT


def test_data_writing_scripts_refuse_the_development_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    with pytest.raises(SystemExit, match="stack_tests.py"):
        local_stack.require_test_stack(dev=False)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", local_stack.DEV_PROJECT)
    with pytest.raises(SystemExit, match="--dev"):
        local_stack.require_test_stack(dev=False)
    local_stack.require_test_stack(dev=True)  # --dev: no check, no docker call


def test_data_writing_scripts_need_the_test_stack_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", local_stack.TEST_PROJECT)
    outputs = iter(["", "4f2a9c\n"])
    monkeypatch.setattr(local_stack.subprocess, "run", lambda *_a, **_k: compose_result(next(outputs)))
    with pytest.raises(SystemExit, match="not running"):
        local_stack.require_test_stack(dev=False)
    local_stack.require_test_stack(dev=False)


def test_the_test_stack_publishes_its_own_ports() -> None:
    """Every published port and every URL the stack hands out follows the port variables, and the test stack's
    differ from the development stack's defaults, so both run at once."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    published = re.findall(r"^    - 127\.0\.0\.1:(\S+):\d+$", compose, re.MULTILINE)
    assert published == ["${KEYCLOAK_PORT:-8081}", "${KONG_PORT:-8000}", "${WEB_PORT:-4200}"]
    assert re.findall(r"http://localhost:(\d+)", compose) == []
    defaults = {"KONG_PORT": "8000", "KEYCLOAK_PORT": "8081", "WEB_PORT": "4200"}
    assert local_stack.TEST_PORTS.keys() == defaults.keys()
    assert set(local_stack.TEST_PORTS.values()).isdisjoint(defaults.values())
    assert f"http://localhost:{local_stack.TEST_PORTS['WEB_PORT']}" == setup.TEST_WEB_ORIGIN
    web = setup.realm(setup.generate_secrets())["clients"][0]
    assert web["webOrigins"] == [setup.WEB_ORIGIN, setup.TEST_WEB_ORIGIN]


def test_scripts_follow_the_port_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, port in local_stack.TEST_PORTS.items():
        monkeypatch.setenv(name, port)
    stack = importlib.reload(local_stack)
    try:
        assert stack.BASE == "http://localhost:18000/api"
        assert stack.TOKEN_URL.startswith("http://localhost:18081/realms/learning/")
        ready = importlib.reload(wait_ready)
        assert [url for url, _ in ready.PUBLIC.values()] == [
            "http://127.0.0.1:18000/api/identity/classrooms",
            "http://127.0.0.1:18081/realms/learning",
        ]
    finally:
        monkeypatch.undo()
        importlib.reload(local_stack)
        importlib.reload(wait_ready)


def test_fresh_volumes_get_every_database_and_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = setup.generate_secrets()
    monkeypatch.setattr(stack_tests, "ROOT", tmp_path)
    monkeypatch.setattr(stack_tests, "env", lambda: values)
    stack_tests.refresh_local_files()
    sql = (tmp_path / ".local/postgres-init.sql").read_text(encoding="utf-8")
    assert all(f"CREATE DATABASE {name} OWNER {name};" in sql for name in setup.SERVICES)
    realm = json.loads((tmp_path / ".local/learning-realm.json").read_text(encoding="utf-8"))
    assert realm == setup.realm(values)


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
