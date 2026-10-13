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
ENV = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if line and not line.startswith("#"))


def psql(database: str, sql: str) -> str:
    return subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", database, "-At", "-c", sql], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()


if psql("postgres", "SELECT count(*) FROM pg_database WHERE datname='content_test'") == "0":
    psql("postgres", "CREATE DATABASE content_test OWNER content")
psql("content_test", "CREATE EXTENSION IF NOT EXISTS vector")
url = f"postgresql://content:{ENV['CONTENT_DB_PASSWORD']}@postgres:5432/content_test?sslmode=disable"
command = ["docker", "run", "--rm", "--network", "learning-foundation_default", "-e", "CONTENT_TEST_DATABASE_URL", "-e", "GOFLAGS=-buildvcs=false", "-e", "GOCACHE=/go/cache",
           "-v", f"{ROOT}:/src:ro", "-v", "learning-go-cache:/go", "-w", "/src/services/content", "golang:1.25.7-alpine", "go", "test", "-count=1", "-v", "./..."]
subprocess.run(command, env={**os.environ, "CONTENT_TEST_DATABASE_URL": url}, check=True)
print("PASS: Content indexing and search against PostgreSQL with pgvector")
