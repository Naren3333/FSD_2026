"""Run the database-backed tests that check.py skips, against the local Compose PostgreSQL.

- The Go modules' store tests (platform outbox, inbox and migrations; Identity, Curriculum, Assessment, Grading and
  Content against real SQL, Content with pgvector), in a Go container.
- The Python services' PostgreSQL tests (ai-core's usage ledger, Analytics, the chatbot's PostgresStore), in each
  service's own image.

Each suite gets its own scratch database, created once and owned by a service's role (`identity_test`,
`grading_test`, ...), so no service's real data is touched; every test then works in a throwaway schema of it. The
tests run in containers on the Compose network because PostgreSQL is not published to the host. ai-core is faked
inside the tests. They use the stack that COMPOSE_PROJECT_NAME names (the development one if unset);
scripts/stack_tests.py runs them in the disposable test stack.
"""

import os
import subprocess

from local_stack import ROOT, env, project, psql_value
from setup import password_key

GO_IMAGE = "golang:1.25.7-alpine"
NETWORK = f"{project()}_default"
PYTEST = "pytest==8.4.1"  # the version in requirements-dev.lock

# Go module directory, its tests' environment variable, the role owning its scratch database, extensions.
GO_SUITES = [
    ("packages/platform", "PLATFORM_TEST_DATABASE_URL", "identity", ()),
    ("services/identity", "IDENTITY_TEST_DATABASE_URL", "identity", ()),
    ("services/curriculum", "CURRICULUM_TEST_DATABASE_URL", "curriculum", ()),
    ("services/assessment", "ASSESSMENT_TEST_DATABASE_URL", "assessment", ()),
    ("services/grading", "GRADING_TEST_DATABASE_URL", "grading", ()),
    ("services/content", "CONTENT_TEST_DATABASE_URL", "content", ("vector",)),
]


def scratch_database(name: str, owner: str, *extensions: str) -> str:
    """Creates `<name>_test` owned by the owner's role if missing; returns its URL on the Compose network. As with
    the services' own databases, no other role may connect to it (a database left by an older run is closed too)."""
    database = f"{name}_test"
    if psql_value(f"SELECT count(*) FROM pg_database WHERE datname='{database}'") == "0":
        psql_value(f"CREATE DATABASE {database} OWNER {owner}")
    psql_value(f"REVOKE ALL ON DATABASE {database} FROM PUBLIC")
    for extension in extensions:
        psql_value(f"CREATE EXTENSION IF NOT EXISTS {extension}", database=database)
    password = env()[password_key(owner)]
    return f"postgresql://{owner}:{password}@postgres:5432/{database}?sslmode=disable"


def go_suite(module: str, variable: str, owner: str, extensions: tuple[str, ...]) -> None:
    url = scratch_database(module.rsplit("/", 1)[-1], owner, *extensions)
    command = [
        "docker", "run", "--rm",
        "--network", NETWORK,
        "-e", variable,
        "-e", "GOFLAGS=-buildvcs=false",
        "-e", "GOCACHE=/go/cache",
        "-v", f"{ROOT}:/src:ro",
        "-v", "learning-go-cache:/go",
        "-w", f"/src/{module}",
        GO_IMAGE, "go", "test", "-count=1", "-v", "./...",
    ]  # fmt: skip
    output = run(command, {variable: url})
    if f"{variable} is not set" in output:
        raise SystemExit(f"{module}'s database tests were skipped")
    print(f"PASS: {module} against PostgreSQL")


def python_suite(service: str, variable: str, owner: str, tests: str) -> None:
    """Runs the service's database tests in its own image, which has its dependencies but not pytest."""
    url = scratch_database(owner, owner)
    directory = ROOT / "services" / service
    test = f"pip install --quiet --disable-pip-version-check {PYTEST} && python -m pytest -q -rs -p no:cacheprovider {tests}"
    command = [
        "docker", "compose", "run", "--rm", "--no-deps", "--user", "root",
        "-e", variable,
        "-v", f"{directory}:/app:ro",
        "--entrypoint", "sh",
        service, "-c", test,
    ]  # fmt: skip
    output = run(command, {variable: url})
    if f"{variable} is not set" in output:
        raise SystemExit(f"{service}'s database tests were skipped")
    print(f"PASS: {service} against PostgreSQL")


def run(command: list[str], extra: dict[str, str]) -> str:
    """Runs a test command, showing its output as it comes, and returns that output; stops the script if it fails."""
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env={**os.environ, **extra},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # Go and pytest write UTF-8; Windows would otherwise decode it with its locale code page.
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    lines = []
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    if process.wait() != 0:
        raise SystemExit(f"Failed: {' '.join(command[:4])} ...")
    return "".join(lines)


# Python service (its Compose name), its tests' environment variable, the role owning its scratch database, the
# test files that use the database.
PYTHON_SUITES = [
    ("question-chatbot-agent", "CHATBOT_TEST_DATABASE_URL", "chatbot", "tests/test_store.py"),
    ("ai-core", "AICORE_TEST_DATABASE_URL", "aicore", "tests/test_ledger.py"),
    ("analytics", "ANALYTICS_TEST_DATABASE_URL", "analytics", "test_store.py"),
]

if __name__ == "__main__":
    for suite in GO_SUITES:
        go_suite(*suite)
    for python in PYTHON_SUITES:
        python_suite(*python)
