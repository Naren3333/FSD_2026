"""run the integration, database and browser tests against a disposable test stack, never the development one."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from local_stack import (
    ROOT,
    TEST_LOCAL_CONFIG_DIR,
    TEST_PORTS,
    TEST_PROJECT,
    TEST_URLS,
    env,
    load_test_monitoring_credentials,
)
from setup import MONITORING_CLIENT, TEST_WEB_ORIGIN, client_secret_key, postgres_init, realm

# each suite's command and working directory, in the order they run.
NPM = "npm.cmd" if os.name == "nt" else "npm"
SUITES: dict[str, tuple[list[str], Path]] = {
    "integration": ([sys.executable, "scripts/integration.py"], ROOT),
    "db": ([sys.executable, "scripts/db_tests.py"], ROOT),
    "e2e": ([NPM, "run", "test:e2e"], ROOT / "apps/web"),
    "scale": ([sys.executable, "scripts/scale_demo.py"], ROOT),
}
# the suites a run without any named runs.
DEFAULT_SUITES = ["integration", "db", "e2e"]
# compose files the scale demo adds: its replicas, cpu caps and the k6 load generator.
SCALE_FILE = "docker-compose.scale.yml"
READY_TIMEOUT = 600  # seconds: a fresh stack imports the realm and runs every migration before it answers
# isolate compose commands, ports and first-start files from the development stack.
TEST_ENV = {
    **os.environ,
    "COMPOSE_PROJECT_NAME": TEST_PROJECT,
    **TEST_PORTS,
    **TEST_URLS,
    "LOCAL_CONFIG_DIR": f"./{TEST_LOCAL_CONFIG_DIR}",
}


def compose(*args: str, check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    """runs `docker compose` on the test stack, showing its output."""
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        env=TEST_ENV,
        text=True,
        check=check,
        capture_output=capture_output,
    )


def render_local_files() -> dict[str, str]:
    """generate the disposable stack's postgresql init and realm import from .env."""
    try:
        values = dict(env())
        key = client_secret_key(MONITORING_CLIENT)
        TEST_ENV[key] = values[key] = secrets.token_hex(20)
        operator_password = TEST_ENV["TEST_OPERATOR_PASSWORD"] = secrets.token_hex(20)
        sql, import_ = (
            postgres_init(values),
            realm(values, TEST_WEB_ORIGIN, operator_password=operator_password),
        )
    except FileNotFoundError:
        raise SystemExit("No .env: run python scripts/setup.py first.") from None
    except KeyError as missing:
        raise SystemExit(f".env lacks {missing}: run python scripts/upgrade_local.py.") from None
    return {"postgres-init.sql": sql, "learning-realm.json": json.dumps(import_, indent=2)}


def write_local_files(files: dict[str, str]) -> None:
    local = ROOT / TEST_LOCAL_CONFIG_DIR
    local.mkdir(parents=True, exist_ok=True)
    for name, contents in files.items():
        (local / name).write_text(contents, encoding="utf-8")


def with_scale_file() -> None:
    """adds docker-compose.scale.yml to the compose files every command of this run uses, after any COMPOSE_FILE already names (compose separates them with the platform's path separator)."""
    files = TEST_ENV.get("COMPOSE_FILE") or "docker-compose.yml"
    TEST_ENV["COMPOSE_FILE"] = os.pathsep.join([files, SCALE_FILE])


def run_suite(name: str, live_ai: bool) -> bool:
    command, cwd = SUITES[name]
    if name == "integration" and live_ai:
        command = [*command, "--live-ai"]
    print(f"\n=== {name} ===", flush=True)
    started = time.monotonic()
    passed = subprocess.run(command, cwd=cwd, env=TEST_ENV, check=False).returncode == 0
    print(f"=== {'PASS' if passed else 'FAIL'} {name} ({time.monotonic() - started:.0f}s) ===", flush=True)
    return passed


def wait_until_ready() -> None:
    ready = [sys.executable, "scripts/wait_ready.py", "--timeout", str(READY_TIMEOUT)]
    if subprocess.run(ready, cwd=ROOT, env=TEST_ENV, check=False).returncode != 0:
        compose("ps", "--all", check=False)
        raise SystemExit(f"The test stack did not become ready; see `docker compose -p {TEST_PROJECT} logs`.")


def fresh_stack(suites: list[str], args: argparse.Namespace) -> list[str]:
    files = render_local_files()
    compose("down", "--volumes", "--remove-orphans")
    write_local_files(files)
    try:
        compose("up", "--detach", *([] if args.no_build else ["--build"]))
        wait_until_ready()
        return [name for name in suites if not run_suite(name, args.live_ai)]
    finally:
        if args.keep:
            print(
                f"\nThe test stack is available at {TEST_WEB_ORIGIN}. "
                "Rerun suites with: python scripts/stack_tests.py --reuse"
            )
        else:
            compose("down", "--volumes", "--remove-orphans", check=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "suites",
        nargs="*",
        metavar="suite",
        help=f"any of {', '.join(SUITES)} (default: {', '.join(DEFAULT_SUITES)})",
    )
    parser.add_argument("--keep", action="store_true", help="leave the test stack running afterwards")
    parser.add_argument("--reuse", action="store_true", help="run against the already-running test stack")
    parser.add_argument("--no-build", action="store_true", help="reuse the images the last run built")
    parser.add_argument(
        "--live-ai", action="store_true", help="pass --live-ai to integration.py (paid calls)"
    )
    args = parser.parse_args()
    unknown = [s for s in args.suites if s not in SUITES]
    if unknown:
        parser.error(f"unknown suite {', '.join(unknown)}; choose from {', '.join(SUITES)}")
    suites = [s for s in SUITES if s in args.suites] if args.suites else DEFAULT_SUITES
    if "scale" in suites:
        with_scale_file()

    if args.reuse:
        TEST_ENV.update(load_test_monitoring_credentials(ROOT))
        if not compose(
            "ps", "--status", "running", "--quiet", "postgres", capture_output=True
        ).stdout.strip():
            raise SystemExit(
                "The test stack is not running. Start it with python scripts/stack_tests.py --keep."
            )
        wait_until_ready()
        failed = [name for name in suites if not run_suite(name, args.live_ai)]
    else:
        failed = fresh_stack(suites, args)
    if failed:
        raise SystemExit(f"FAIL: {', '.join(failed)} (against the {TEST_PROJECT} stack)")
    print(f"\nPASS: {', '.join(suites)} against the {TEST_PROJECT} stack")


if __name__ == "__main__":
    main()
