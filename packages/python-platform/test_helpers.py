import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from learning_transport import (
    RELATION_STUDENT,
    Claims,
    IdentityClient,
    ServiceToken,
    UpstreamError,
    actor,
    application,
    caller,
    read_events,
    require_relation,
    requires,
    sse_event,
)
from learning_transport.kafka import Envelope, parse
from learning_transport.logs import JsonFormatter
from learning_transport.testing import as_caller, token_claims


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def claims(*roles: str) -> Claims:
    return Claims.from_token(token_claims(*roles, sub="u1", org="o1"))


def test_person_role_is_the_accounts_one_role() -> None:
    assert claims("teacher").person_role == "teacher"
    assert claims("student").person_role == "student"
    assert claims("ai-client").person_role is None


def test_requires_lets_any_listed_role_through() -> None:
    check = requires("teacher", "student")
    assert check(claims("student")).subject == "u1"
    with pytest.raises(HTTPException) as error:
        check(claims("ai-client"))
    assert error.value.status_code == 403


def test_a_token_with_both_person_roles_is_refused() -> None:
    with pytest.raises(HTTPException) as error:
        caller(token_claims("teacher", "student", sub="u1", org="o1"))
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "ROLE_CONFLICT"  # type: ignore[index]
    assert caller(token_claims("teacher", "ai-client", sub="u1", org="o1")).person_role == "teacher"


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


@pytest.mark.parametrize(
    ("status", "body", "allowed", "expected"),
    [
        (200, {"relation": "student"}, ("teacher", "student"), ("student", None)),
        (200, {"relation": "student"}, ("teacher",), (None, (403, "DENIED"))),
        (403, None, ("teacher", "student"), (None, (403, "DENIED"))),
        (503, None, ("teacher", "student"), (None, (503, "DEPENDENCY_UNAVAILABLE"))),
    ],
)
def test_require_relation_maps_denial_to_403_and_an_outage_to_503(
    status: int, body: dict[str, Any] | None, allowed: tuple[str, ...], expected: tuple[Any, Any]
) -> None:
    relation, error = expected
    request = SimpleNamespace(
        headers={"authorization": "Bearer t"}, state=SimpleNamespace(correlation_id="corr")
    )
    client = identity(status, body)
    check = require_relation(client, request, "c1", allowed=allowed, denied=("DENIED", "no"))  # type: ignore[arg-type]
    if error is None:
        assert run(check) == relation
        return
    with pytest.raises(HTTPException) as raised:
        run(check)
    assert (raised.value.status_code, raised.value.detail["code"]) == error  # type: ignore[index]


class Payload(BaseModel):
    n: int


class Event(Envelope):
    data: Payload


EVENT = {
    "event_id": "00000000-0000-4000-8000-000000000001",
    "event_type": "test.v1",
    "schema_version": 1,
    "timestamp": "2026-10-15T00:00:00+08:00",
    "correlation_id": "00000000-0000-4000-8000-000000000002",
    "resource_id": "00000000-0000-4000-8000-000000000003",
    "data": {"n": 1},
}


def test_an_envelope_parses_or_is_none_for_dead_lettering() -> None:
    event = parse(Event, json.dumps(EVENT).encode())
    assert event is not None and event.data.n == 1
    for broken in (
        {**EVENT, "timestamp": "2026-10-15T00:00:00"},  # no time zone
        {**EVENT, "schema_version": 2},
        {**EVENT, "extra": True},
        {k: v for k, v in EVENT.items() if k != "event_id"},
    ):
        assert parse(Event, json.dumps(broken).encode()) is None
    assert parse(Event, None) is None and parse(Event, b"not json") is None


def test_as_caller_overrides_the_verified_claims_for_the_block() -> None:
    app = application("test-app")

    @app.get("/me")
    def me(claims: Claims = Depends(caller)) -> dict[str, Any]:
        return {"sub": claims.subject, "org": claims.org, "roles": sorted(claims.roles)}

    client = TestClient(app)
    with as_caller(app, token_claims("teacher", sub="t1", org="o9")):
        assert client.get("/me").json() == {"sub": "t1", "org": "o9", "roles": ["teacher"]}
    assert actor not in app.dependency_overrides
    assert client.get("/me").status_code == 401


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


def test_calls_queued_behind_a_failed_token_fetch_fail_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """keycloak hanging: the first call waits out the fetch, the ones queued behind it fail without asking again, and after failure_ttl the next call tries keycloak again."""
    clock = [100.0]
    monkeypatch.setattr("learning_transport.tokens._now", lambda: clock[0])
    calls = 0

    async def answer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.05)
            clock[0] += 10  # the fetch timed out
            raise httpx.ReadTimeout("hung", request=request)
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 300})

    async def scenario() -> tuple[list[Any], str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            token = ServiceToken(http, "http://kc/token", "id", "secret")
            failed = await asyncio.gather(*(token.get() for _ in range(5)), return_exceptions=True)
            clock[0] += 6  # past failure_ttl
            return failed, await token.get()

    failed, fresh = run(scenario())
    assert all(isinstance(f, UpstreamError) and f.code == "TOKEN_UNAVAILABLE" for f in failed)
    assert calls == 2 and fresh == "fresh"


def test_a_request_that_crashes_is_logged_with_its_traceback(caplog: pytest.LogCaptureFixture) -> None:
    app = application("crash-app")

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR, logger="crash-app"):
        resp = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert resp.status_code == 500 and resp.json()["error"]["code"] == "INTERNAL_ERROR"
    [record] = [r for r in caplog.records if r.getMessage() == "request_failed"]
    assert record.exc_info is not None and "kaboom" in JsonFormatter("crash-app").format(record)


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


def roster_client(status: int, body: Any) -> IdentityClient:
    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://identity/v1/classrooms/c1/student-ids"
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(status, json=body)

    return IdentityClient("http://identity/", httpx.AsyncClient(transport=httpx.MockTransport(answer)))


def test_identity_roster_is_the_students_ids_in_order() -> None:
    assert run(roster_client(200, ["s2", "s1"]).roster("Bearer t", "c1", "corr")) == ["s2", "s1"]
    assert run(roster_client(403, {}).roster("Bearer t", "c1", "corr")) is None
    for status, body in [(503, {}), (200, {"students": []}), (200, [{"id": "s1"}]), (200, [""])]:
        with pytest.raises(UpstreamError):
            run(roster_client(status, body).roster("Bearer t", "c1", "corr"))
