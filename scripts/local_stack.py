"""Helpers the local-stack scripts share: the generated .env, sign-in through the local Keycloak, its admin API, and
PostgreSQL inside the Compose container. Local development stack only; the Keycloak calls need httpx.

Scripts in scripts/ import this directly (`python scripts/<name>.py` puts scripts/ on the path); scripts/eval/common.py
adds scripts/ to the path first."""

import json
import os
import subprocess
from collections.abc import Iterable, Iterator
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from setup import ORG

if TYPE_CHECKING:
    import httpx

ROOT = Path(__file__).resolve().parents[1]
# The published ports, as docker-compose.yml reads them: the development stack's unless the environment names others
# (scripts/stack_tests.py sets TEST_PORTS for the test stack).
KONG_PORT = os.environ.get("KONG_PORT") or "8000"
KEYCLOAK_PORT = os.environ.get("KEYCLOAK_PORT") or "8081"
BASE = f"http://localhost:{KONG_PORT}/api"
KEYCLOAK = f"http://localhost:{KEYCLOAK_PORT}"
TOKEN_URL = f"{KEYCLOAK}/realms/learning/protocol/openid-connect/token"
ADMIN_TOKEN_URL = f"{KEYCLOAK}/realms/master/protocol/openid-connect/token"
PSQL_TIMEOUT = 90  # seconds
# Compose projects of docker-compose.yml. The development stack is the file's own name; the test stack is a second
# project with its own containers, network, volumes and published ports, made fresh for each run by
# scripts/stack_tests.py, so it runs beside the development stack.
DEV_PROJECT = "learning-foundation"
TEST_PROJECT = "learning-test"
TEST_PORTS = {"KONG_PORT": "18000", "KEYCLOAK_PORT": "18081", "WEB_PORT": "14200"}


def read_env(path: Path = ROOT / ".env") -> dict[str, str]:
    """The KEY=value lines of a .env file, skipping comments and lines without a value."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


@cache
def env() -> dict[str, str]:
    """The repository's .env, made by scripts/setup.py, read once."""
    return read_env()


def project() -> str:
    """The Compose project that `docker compose` acts on here: COMPOSE_PROJECT_NAME, which Compose itself reads
    first, or else the development stack."""
    return os.environ.get("COMPOSE_PROJECT_NAME") or DEV_PROJECT


def require_test_stack(*, dev: bool) -> None:
    """Stops a script that creates and deletes data unless it is aimed at the running test stack, or `dev` (the
    script's --dev flag) says to use the development stack's data anyway."""
    if dev:
        return
    if project() != TEST_PROJECT:
        raise SystemExit(
            "This creates and deletes data, so it runs against the disposable test stack: "
            "python scripts/stack_tests.py. Pass --dev to run it against the development stack instead."
        )
    # The HTTP calls go to these ports, read when this module loads: the test project name alone would send psql to
    # the test stack but requests to the development stack's Kong and Keycloak.
    published = {"KONG_PORT": KONG_PORT, "KEYCLOAK_PORT": KEYCLOAK_PORT}
    wrong = [f"{name}={port}" for name, port in published.items() if port != TEST_PORTS[name]]
    if wrong:
        raise SystemExit(
            f"COMPOSE_PROJECT_NAME is {TEST_PROJECT}, but {', '.join(wrong)} is not the test stack's port "
            f"({', '.join(f'{name}={TEST_PORTS[name]}' for name in published)}). "
            "Run it through python scripts/stack_tests.py."
        )
    running = subprocess.run(
        ["docker", "compose", "ps", "--status", "running", "--quiet", "postgres"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=PSQL_TIMEOUT,
    )
    if not running.stdout.strip():
        raise SystemExit(
            f"The {TEST_PROJECT} stack is not running. Start it with python scripts/stack_tests.py."
        )


def check(response: "httpx.Response") -> "httpx.Response":
    """The response, or exit with what went wrong."""
    if response.is_error:
        request = response.request
        raise SystemExit(
            f"{request.method} {request.url.path} -> {response.status_code}: {response.text[:300]}"
        )
    return response


def password_token(username: str, password: str) -> str:
    """A local person's access token, from the learning-e2e client's password grant (tests and evals only)."""
    import httpx  # noqa: PLC0415 - only the scripts that sign in need httpx

    data = {"client_id": "learning-e2e", "grant_type": "password", "username": username, "password": password}
    res = httpx.post(TOKEN_URL, data=data, timeout=15)
    if res.status_code != 200:
        raise SystemExit(f"Local sign-in failed for {username}: {res.status_code}")
    return str(res.json()["access_token"])


def admin_client() -> "httpx.Client":
    """The learning realm's admin API as the local Keycloak admin. Admin tokens last a minute: open a new client for
    each batch of work."""
    import httpx  # noqa: PLC0415 - only the scripts that use the admin API need httpx

    data = {
        "client_id": "admin-cli",
        "grant_type": "password",
        "username": "admin",
        "password": env()["KEYCLOAK_ADMIN_PASSWORD"],
    }
    token = check(httpx.post(ADMIN_TOKEN_URL, data=data, timeout=15)).json()["access_token"]
    return httpx.Client(
        base_url=f"{KEYCLOAK}/admin/realms/learning", headers={"Authorization": "Bearer " + token}, timeout=15
    )


def ensure_person(  # noqa: PLR0913 - one local person's whole Keycloak record
    admin: "httpx.Client",
    username: str,
    first: str,
    last: str,
    password: str,
    *,
    role: str = "student",
    reset_password: bool = True,
) -> str:
    """Creates the person with `password`, or (with reset_password) gives an existing one that password, then the
    realm role and the local organization. Returns their Keycloak id. org_id is an unmanaged attribute that the
    admin API only keeps while the realm lets admins edit those, so that is allowed just long enough to set it."""
    found = check(admin.get("/users", params={"username": username, "exact": "true"})).json()
    credential = {"type": "password", "value": password, "temporary": False}
    if found:
        user_id = str(found[0]["id"])
        if reset_password:
            check(admin.put(f"/users/{user_id}/reset-password", json=credential))
    else:
        user = {
            "username": username,
            "enabled": True,
            "emailVerified": True,
            "firstName": first,
            "lastName": last,
            "email": f"{username}@example.test",
            "credentials": [credential],
        }
        check(admin.post("/users", json=user))
        user_id = str(
            check(admin.get("/users", params={"username": username, "exact": "true"})).json()[0]["id"]
        )
    realm_role = check(admin.get(f"/roles/{role}")).json()
    check(admin.post(f"/users/{user_id}/role-mappings/realm", json=[realm_role]))
    profile = check(admin.get("/users/profile")).json()
    check(admin.put("/users/profile", json={**profile, "unmanagedAttributePolicy": "ADMIN_EDIT"}))
    try:
        user = check(admin.get(f"/users/{user_id}")).json()
        attributes = {**(user.get("attributes") or {}), "org_id": [ORG]}
        check(admin.put(f"/users/{user_id}", json={**user, "attributes": attributes}))
    finally:
        check(admin.put("/users/profile", json=profile))
    return user_id


def psql(
    sql: str, *, database: str = "postgres", user: str = "postgres", check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Runs one statement in the Compose PostgreSQL as `user` (the superuser unless a service's own role is named):
    unaligned, tuples only, tab-separated columns."""
    command = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", user, "-d", database]
    command += ["-v", "ON_ERROR_STOP=1", "-At", "-F", "\t", "-c", sql]
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=check,
        timeout=PSQL_TIMEOUT,
    )


def psql_value(sql: str, *, database: str = "postgres", user: str = "postgres") -> str:
    """The single value a statement returns."""
    return psql(sql, database=database, user=user).stdout.strip()


def delete_topics(where: str) -> str:
    """Deletes the topics a condition on Curriculum's `topics` picks, with their prerequisite links, straight from
    Curriculum's database. Test data only: an assessment or grade that named a deleted topic shows its id instead.
    Returns how many were deleted. apps/web/e2e/helpers.ts deleteTestTopic runs the same statement."""
    ids = f"(SELECT id FROM topics WHERE {where})"
    return psql_value(
        f"WITH p AS (DELETE FROM prerequisites WHERE topic_id IN {ids} OR prerequisite_id IN {ids}), "
        f"d AS (DELETE FROM topics WHERE id IN {ids} RETURNING 1) SELECT count(*) FROM d",
        database="curriculum",
        user="curriculum",
    )


def psql_json(sql: str, *, database: str = "postgres", user: str = "postgres") -> list[dict[str, Any]]:
    """The rows a SELECT returns, each a dict by column name. PostgreSQL encodes them as one JSON array, so text
    holding tabs, line breaks or anything else comes back intact."""
    out = psql_value(f"SELECT coalesce(json_agg(r), '[]') FROM ({sql}) r", database=database, user=user)
    rows: list[dict[str, Any]] = json.loads(out)
    return rows


def sse_events(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """(event name, JSON data) for each frame of a server-sent event stream, such as httpx's `iter_lines()`. The
    synchronous twin of learning_transport.read_events, which the scripts don't import (they run without the
    services' packages on the path)."""
    event = ""
    for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            yield event, json.loads(line[5:])
