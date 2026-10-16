"""Run the database-backed tests that check.py skips, against the local Compose PostgreSQL.

- Content's Go tests (indexing worker, duplicate and stale jobs, hybrid search) with pgvector, in a Go container.
- The chatbot's PostgresStore tests, in the chatbot's own image.

Each suite gets its own scratch database, created once and owned by the service's role (`content_test`,
`chatbot_test`), so no service's real data is touched. The tests run in containers on the Compose network because
PostgreSQL is not published to the host. ai-core is faked inside the tests.
"""

import os
import subprocess

from local_stack import ROOT, env, psql_value
from setup import password_key

GO_IMAGE = "golang:1.25.7-alpine"
NETWORK = "learning-foundation_default"
PYTEST = "pytest==8.4.1"  # the version in requirements-dev.lock


def scratch_database(service: str, *extensions: str) -> str:
    """Creates `<service>_test` owned by the service's role if missing; returns its URL on the Compose network."""
    name = f"{service}_test"
    if psql_value(f"SELECT count(*) FROM pg_database WHERE datname='{name}'") == "0":
        psql_value(f"CREATE DATABASE {name} OWNER {service}")
    for extension in extensions:
        psql_value(f"CREATE EXTENSION IF NOT EXISTS {extension}", database=name)
    password = env()[password_key(service)]
    return f"postgresql://{service}:{password}@postgres:5432/{name}?sslmode=disable"


def content() -> None:
    url = scratch_database("content", "vector")
    command = [
        "docker", "run", "--rm",
        "--network", NETWORK,
        "-e", "CONTENT_TEST_DATABASE_URL",
        "-e", "GOFLAGS=-buildvcs=false",
        "-e", "GOCACHE=/go/cache",
        "-v", f"{ROOT}:/src:ro",
        "-v", "learning-go-cache:/go",
        "-w", "/src/services/content",
        GO_IMAGE, "go", "test", "-count=1", "-v", "./...",
    ]  # fmt: skip
    output = run(command, {"CONTENT_TEST_DATABASE_URL": url})
    if "--- SKIP" in output:
        raise SystemExit("Content's database tests were skipped")
    print("PASS: Content indexing and search against PostgreSQL with pgvector")


def chatbot() -> None:
    url = scratch_database("chatbot")
    service = ROOT / "services" / "question-chatbot-agent"
    test = f"pip install --quiet --disable-pip-version-check {PYTEST} && python -m pytest -q -rs -p no:cacheprovider tests/test_store.py"
    command = [
        "docker", "compose", "run", "--rm", "--no-deps", "--user", "root",
        "-e", "CHATBOT_TEST_DATABASE_URL",
        "-v", f"{service}:/app:ro",
        "--entrypoint", "sh",
        "question-chatbot-agent", "-c", test,
    ]  # fmt: skip
    output = run(command, {"CHATBOT_TEST_DATABASE_URL": url})
    if "skipped" in output:
        raise SystemExit("The chatbot's store tests were skipped")
    print("PASS: the chatbot's PostgresStore against PostgreSQL")


def run(command: list[str], extra: dict[str, str]) -> str:
    """Runs a test command, showing its output as it comes, and returns that output; stops the script if it fails."""
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env={**os.environ, **extra},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    lines = []
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    if process.wait() != 0:
        raise SystemExit(f"Failed: {' '.join(command[:4])} ...")
    return "".join(lines)


if __name__ == "__main__":
    content()
    chatbot()
