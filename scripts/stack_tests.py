"""Run the integration, database and browser tests against a disposable test stack, never the development one.

The test stack is a second Compose project of docker-compose.yml, `learning-test`, with its own containers, network,
volumes and ports (Kong 18000, Keycloak 18081, the web app 14200; local_stack.TEST_PORTS), so it runs beside a
development stack without touching it. Its databases, Kafka topics, documents and Keycloak start empty but for the
seeded local accounts, from the same .env. Each run creates it fresh, runs the suites in order and removes it with
its volumes; --keep leaves it running to look at afterwards (the next run replaces it).

    python scripts/stack_tests.py                     # integration, db and e2e
    python scripts/stack_tests.py integration --live-ai
    python scripts/stack_tests.py e2e --keep
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from local_stack import ROOT, TEST_PORTS, TEST_PROJECT, env
from setup import postgres_init, realm

# Each suite's command and working directory, in the order they run.
NPM = "npm.cmd" if os.name == "nt" else "npm"
SUITES: dict[str, tuple[list[str], Path]] = {
    "integration": ([sys.executable, "scripts/integration.py"], ROOT),
    "db": ([sys.executable, "scripts/db_tests.py"], ROOT),
    "e2e": ([NPM, "run", "test:e2e"], ROOT / "apps/web"),
}
READY_TIMEOUT = 600  # seconds: a fresh stack imports the realm and runs every migration before it answers
# Every `docker compose` command here and in the test scripts acts on the project this names (Compose reads it before
# the file's `name:`) and publishes the test ports; integration.py and the browser tests refuse to run without it,
# and reach the stack on those ports.
TEST_ENV = {**os.environ, "COMPOSE_PROJECT_NAME": TEST_PROJECT, **TEST_PORTS}


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Runs `docker compose` on the test stack, showing its output."""
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, env=TEST_ENV, text=True, check=check)


def refresh_local_files() -> None:
    """Writes .local's PostgreSQL init script and realm import from .env again, as scripts/setup.py made them.
    Both are read only when a volume is created, so the development stack is unchanged, but the fresh test volumes
    get every current database and client even when the files predate them (upgrade_local.py adds those to a running
    stack and rewrites only the realm)."""
    try:
        values = env()
        sql, import_ = postgres_init(values), realm(values)
    except FileNotFoundError:
        raise SystemExit("No .env: run python scripts/setup.py first.") from None
    except KeyError as missing:
        raise SystemExit(f".env lacks {missing}: run python scripts/upgrade_local.py.") from None
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    (local / "postgres-init.sql").write_text(sql, encoding="utf-8")
    (local / "learning-realm.json").write_text(json.dumps(import_, indent=2), encoding="utf-8")


def run_suite(name: str, live_ai: bool) -> bool:
    command, cwd = SUITES[name]
    if name == "integration" and live_ai:
        command = [*command, "--live-ai"]
    print(f"\n=== {name} ===", flush=True)
    started = time.monotonic()
    passed = subprocess.run(command, cwd=cwd, env=TEST_ENV, check=False).returncode == 0
    print(f"=== {'PASS' if passed else 'FAIL'} {name} ({time.monotonic() - started:.0f}s) ===", flush=True)
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "suites", nargs="*", metavar="suite", help=f"any of {', '.join(SUITES)} (default: all)"
    )
    parser.add_argument("--keep", action="store_true", help="leave the test stack running afterwards")
    parser.add_argument("--no-build", action="store_true", help="reuse the images the last run built")
    parser.add_argument(
        "--live-ai", action="store_true", help="pass --live-ai to integration.py (paid calls)"
    )
    args = parser.parse_args()
    unknown = [s for s in args.suites if s not in SUITES]
    if unknown:
        parser.error(f"unknown suite {', '.join(unknown)}; choose from {', '.join(SUITES)}")
    suites = [s for s in SUITES if s in args.suites] if args.suites else list(SUITES)

    refresh_local_files()
    # Whatever an earlier --keep or interrupted run left, volumes included: every run starts from empty.
    compose("down", "--volumes", "--remove-orphans")
    failed: list[str] = []
    try:
        compose("up", "--detach", *([] if args.no_build else ["--build"]))
        ready = [sys.executable, "scripts/wait_ready.py", "--timeout", str(READY_TIMEOUT)]
        if subprocess.run(ready, cwd=ROOT, env=TEST_ENV, check=False).returncode != 0:
            compose("ps", "--all", check=False)
            raise SystemExit(
                f"The test stack did not become ready; see `docker compose -p {TEST_PROJECT} logs`."
            )
        failed = [name for name in suites if not run_suite(name, args.live_ai)]
    finally:
        if args.keep:
            web = f"http://localhost:{TEST_PORTS['WEB_PORT']}"
            print(
                f"\nThe test stack is still running at {web}. Remove it with: docker compose -p {TEST_PROJECT} down -v"
            )
        else:
            compose("down", "--volumes", "--remove-orphans", check=False)
    if failed:
        raise SystemExit(f"FAIL: {', '.join(failed)} (against the {TEST_PROJECT} stack)")
    print(f"\nPASS: {', '.join(suites)} against a fresh {TEST_PROJECT} stack")


if __name__ == "__main__":
    main()
