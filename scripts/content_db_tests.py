"""Run Content's database-backed Go tests (indexing worker, duplicate and stale jobs, hybrid search) against the
local Compose PostgreSQL with pgvector.

Creates a separate `content_test` database once (owned by the content role, with the vector extension); each test
works in its own throwaway schema there, so Content's real data is never touched. The tests run in a Go container
on the Compose network because PostgreSQL is not published to the host. ai-core is faked inside the tests.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if line and not line.startswith("#")
)
GO_IMAGE = "golang:1.25.7-alpine"


def psql(database: str, sql: str) -> str:
    """Runs one statement as the PostgreSQL superuser, inside the Compose container."""
    command = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres"]
    command += ["-d", database, "-At", "-c", sql]
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()


if psql("postgres", "SELECT count(*) FROM pg_database WHERE datname='content_test'") == "0":
    psql("postgres", "CREATE DATABASE content_test OWNER content")
psql("content_test", "CREATE EXTENSION IF NOT EXISTS vector")
url = f"postgresql://content:{ENV['CONTENT_DB_PASSWORD']}@postgres:5432/content_test?sslmode=disable"
command = [
    "docker", "run", "--rm",
    "--network", "learning-foundation_default",
    "-e", "CONTENT_TEST_DATABASE_URL",
    "-e", "GOFLAGS=-buildvcs=false",
    "-e", "GOCACHE=/go/cache",
    "-v", f"{ROOT}:/src:ro",
    "-v", "learning-go-cache:/go",
    "-w", "/src/services/content",
    GO_IMAGE, "go", "test", "-count=1", "-v", "./...",
]  # fmt: skip
subprocess.run(command, env={**os.environ, "CONTENT_TEST_DATABASE_URL": url}, check=True)
print("PASS: Content indexing and search against PostgreSQL with pgvector")
