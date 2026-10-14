import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from learning_transport import (
    RELATION_STUDENT,
    Claims,
    IdentityClient,
    ServiceToken,
    UpstreamError,
    read_events,
    requires,
    sse_event,
)
from learning_transport.logs import JsonFormatter


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def claims(*roles: str) -> Claims:
    return Claims.from_token({"sub": "u1", "org_id": "o1", "realm_access": {"roles": list(roles)}})


def test_the_teacher_role_wins_for_an_account_with_both() -> None:
    assert claims("student", "teacher").person_role == "teacher"
    assert claims("student").person_role == "student"
    assert claims("ai-client").person_role is None


def test_requires_lets_any_listed_role_through() -> None:
    check = requires("teacher", "student")
    assert check(claims("student")).subject == "u1"
    with pytest.raises(HTTPException) as error:
        check(claims("ai-client"))
    assert error.value.status_code == 403


def identity(status: int, body: dict[str, Any] | None = None) -> IdentityClient:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body or {})

    client = IdentityClient("http://identity/", httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    client.seen = seen  # type: ignore[attr-defined]
    return client


def test_identity_relation_forwards_the_users_token() -> None:
    client = identity(200, {"allowed": True, "relation": "student"})
    assert run(client.relation("Bearer t", "c1", "corr", student_id="s1")) == RELATION_STUDENT
    [request] = client.seen  # type: ignore[attr-defined]
    assert str(request.url) == "http://identity/v1/classrooms/c1/access?student_id=s1"
    assert request.headers["authorization"] == "Bearer t" and request.headers["x-correlation-id"] == "corr"


def test_identity_denial_is_none_and_an_outage_raises() -> None:
    assert run(identity(403).relation("Bearer t", "c1", "corr")) is None
    with pytest.raises(UpstreamError) as error:
        run(identity(503).relation("Bearer t", "c1", "corr"))
    assert (error.value.service, error.value.status) == ("identity", 503)


def test_a_service_token_is_cached_until_reset() -> None:
    calls = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": f"t{calls}", "expires_in": 300})

    async def scenario() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            token = ServiceToken(http, "http://kc/token", "id", "secret")
            first, second = await token.get(), await token.get()
            token.reset()
            return [first, second, await token.get()]

    assert run(scenario()) == ["t1", "t1", "t2"]


def test_a_refused_token_request_is_an_upstream_error() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(401))
        async with httpx.AsyncClient(transport=transport) as http:
            await ServiceToken(http, "http://kc/token", "id", "secret").get()

    with pytest.raises(UpstreamError) as error:
        run(scenario())
    assert error.value.code == "TOKEN_UNAVAILABLE"


def test_events_read_back_as_written() -> None:
    frames = sse_event("delta", {"text": "Hi"}) + sse_event("done", {"usage_tokens": 3})

    async def lines() -> AsyncIterator[str]:
        for line in frames.splitlines():
            yield line

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [item async for item in read_events(lines())]

    assert run(collect()) == [("delta", {"text": "Hi"}), ("done", {"usage_tokens": 3})]


def test_log_lines_are_json_with_the_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"name": "svc.part", "levelno": logging.ERROR, "levelname": "ERROR", "msg": "ledger_write_failed"}
    )
    record.model = "m1"
    line = json.loads(JsonFormatter("svc").format(record))
    assert line == {
        "service": "svc",
        "level": "error",
        "logger": "svc.part",
        "message": "ledger_write_failed",
        "model": "m1",
    }


def test_migrations_run_in_version_order(tmp_path: Path) -> None:
    from learning_transport.db import migrations

    (tmp_path / "002_more.sql").write_text("SELECT 2;")
    (tmp_path / "001_init.sql").write_text("SELECT 1;")
    (tmp_path / "notes.txt").write_text("ignored")
    assert [(m.version, m.sql) for m in migrations(tmp_path)] == [(1, "SELECT 1;"), (2, "SELECT 2;")]


@pytest.mark.parametrize("names", [[], ["001_init.sql", "003_gap.sql"], ["001_init.sql", "002-bad.sql"]])
def test_migrations_reject_gaps_and_bad_names(tmp_path: Path, names: list[str]) -> None:
    from learning_transport.db import migrations

    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(ValueError):
        migrations(tmp_path)
