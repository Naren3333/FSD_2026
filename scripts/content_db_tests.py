"""Run Content's database-backed Go tests (indexing worker, duplicate and stale jobs, hybrid search) against the
local Compose PostgreSQL with pgvector.

Creates a separate `content_test` database once (owned by the content role, with the vector extension); each test
works in its own throwaway schema there, so Content's real data is never touched. The tests run in a Go container
on the Compose network because PostgreSQL is not published to the host. ai-core is faked inside the tests.
"""

import os
import subprocess

from local_stack import ROOT, env, psql_value
from setup import password_key

GO_IMAGE = "golang:1.25.7-alpine"

if psql_value("SELECT count(*) FROM pg_database WHERE datname='content_test'") == "0":
    psql_value("CREATE DATABASE content_test OWNER content")
psql_value("CREATE EXTENSION IF NOT EXISTS vector", database="content_test")
url = f"postgresql://content:{env()[password_key('content')]}@postgres:5432/content_test?sslmode=disable"
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
