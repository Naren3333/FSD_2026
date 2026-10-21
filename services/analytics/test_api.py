"""the http routes, with identity and the database replaced by fakes; the real queries run in test_store.py."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from learning_transport import UpstreamError
from learning_transport.testing import as_caller, token_claims

from main import app

ROOM = str(uuid4())
TOPIC = str(uuid4())
AUTH = {"Authorization": "Bearer token"}


class FakeIdentity:
    """answers every access check with one relation (none denies), and the roster with `students`; `down` makes identity unavailable."""

    def __init__(self, relation: str | None, students: list[str] | None = None, down: bool = False) -> None:
        self.relation_answer, self.students, self.down = relation, students, down
        self.asked: list[str | None] = []

    async def relation(
        self, authorization: str, classroom_id: str, correlation: str, student_id: str | None = None
    ) -> str | None:
        self.asked.append(student_id)
        if self.down:
            raise UpstreamError("identity", 0)
        return self.relation_answer

    async def roster(self, authorization: str, classroom_id: str, correlation: str) -> list[str] | None:
        if self.down:
            raise UpstreamError("identity", 0)
        return self.students


class FakePool:
    """a pool whose connection returns `rows` for any query and records the queries' parameters."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[Any] = []

    def connection(self) -> "FakePool":
        return self

    def __enter__(self) -> "FakePool":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> "FakePool":
        self.queries.append(params)
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


@pytest.fixture
def serve() -> Iterator[Any]:
    """sets the app's identity and pool, and returns a client calling as the given roles and subject."""
    client = TestClient(app)

    def set_up(identity: FakeIdentity, pool: FakePool) -> TestClient:
        app.state.identity, app.state.pool = identity, pool
        return client

    yield set_up
    for name in ("identity", "pool"):
        if hasattr(app.state, name):
            delattr(app.state, name)


def evidence(student: str, correct: bool) -> dict[str, Any]:
    return {
        "student_id": student,
        "topic_id": TOPIC,
        "correct": correct,
        "awarded": int(correct),
        "possible": 1,
    }


def test_a_page_past_the_roster_is_empty_and_reads_nothing(serve):
    pool = FakePool()
    client = serve(FakeIdentity("teacher", ["s1", "s2", "s3"]), pool)
    with as_caller(app, token_claims("teacher", sub="t1", org="org")):
        result = client.get("/v1/performance", params={"classroom_id": ROOM, "offset": 5}, headers=AUTH)
    assert result.status_code == 200
    assert result.json()["students"] == [] and result.json()["total"] == 3 and result.json()["offset"] == 5
    assert pool.queries == []


def test_a_page_of_the_roster_reads_only_its_students(serve):
    pool = FakePool([evidence("s2", True)])
    client = serve(FakeIdentity("teacher", ["s1", "s2", "s3"]), pool)
    with as_caller(app, token_claims("teacher", sub="t1", org="org")):
        result = client.get(
            "/v1/performance", params={"classroom_id": ROOM, "offset": 1, "limit": 1}, headers=AUTH
        )
    assert result.status_code == 200
    assert [s["student_id"] for s in result.json()["students"]] == ["s2"]
    assert result.json()["students"][0]["topics"][0]["attempted"] == 1
    assert pool.queries[0][0] == "org" and pool.queries[0][2] == ["s2"]


@pytest.mark.parametrize(
    ("identity", "status"),
    [
        (FakeIdentity("teacher", None), 403),  # identity refused the roster
        (FakeIdentity("student", ["s1"]), 403),  # not this classroom's teacher
        (FakeIdentity(None, ["s1"]), 403),
        (FakeIdentity("teacher", ["s1"], down=True), 503),
    ],
)
def test_the_class_page_is_for_its_teacher_only(serve, identity, status):
    pool = FakePool()
    client = serve(identity, pool)
    with as_caller(app, token_claims("teacher", sub="t1", org="org")):
        result = client.get("/v1/performance", params={"classroom_id": ROOM}, headers=AUTH)
    assert result.status_code == status
    assert pool.queries == []


def test_students_cannot_read_the_class_page(serve):
    client = serve(FakeIdentity("student", ["s1"]), FakePool())
    with as_caller(app, token_claims("student", sub="s1", org="org")):
        assert client.get("/v1/performance", params={"classroom_id": ROOM}, headers=AUTH).status_code == 403


def test_a_roster_outage_is_503(serve):
    class RosterDown(FakeIdentity):
        async def roster(self, authorization: str, classroom_id: str, correlation: str) -> list[str] | None:
            raise UpstreamError("identity", 502)

    client = serve(RosterDown("teacher"), FakePool())
    with as_caller(app, token_claims("teacher", sub="t1", org="org")):
        result = client.get("/v1/performance", params={"classroom_id": ROOM}, headers=AUTH)
    assert result.status_code == 503 and result.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_a_student_without_evidence_has_none_reported(serve):
    client = serve(FakeIdentity("student"), FakePool([]))
    with as_caller(app, token_claims("student", sub="s1", org="org")):
        result = client.get("/v1/performance/s1", params={"classroom_id": ROOM}, headers=AUTH)
    assert result.status_code == 200
    assert result.json()["evidence_status"] == "no_finalized_evidence" and result.json()["topics"] == []


def test_a_student_reads_their_own_topics(serve):
    pool = FakePool([evidence("s1", n < 3) for n in range(5)])
    identity = FakeIdentity("student")
    client = serve(identity, pool)
    with as_caller(app, token_claims("student", sub="s1", org="org")):
        result = client.get("/v1/performance/s1", params={"classroom_id": ROOM}, headers=AUTH)
    assert result.status_code == 200 and result.json()["evidence_status"] == "available"
    assert result.json()["topics"][0]["observed_accuracy"] == 0.6
    assert identity.asked == ["s1"] and pool.queries[0] == ("org", pool.queries[0][1], "s1")


def test_a_student_cannot_read_a_classmate(serve):
    identity = FakeIdentity("student")
    client = serve(identity, FakePool())
    with as_caller(app, token_claims("student", sub="s1", org="org")):
        result = client.get("/v1/performance/s2", params={"classroom_id": ROOM}, headers=AUTH)
    assert result.status_code == 403 and identity.asked == []


def test_a_teacher_account_reported_as_a_student_cannot_read_a_classmate(serve):
    """identity never says this for a teacher's account; if it ever did, a teacher's token alone must not turn a student's access into a teacher's."""
    pool = FakePool([evidence("s2", True)])
    client = serve(FakeIdentity("student"), pool)
    with as_caller(app, token_claims("teacher", sub="t1", org="org")):
        result = client.get("/v1/performance/s2", params={"classroom_id": ROOM}, headers=AUTH)
    assert result.status_code == 403
    assert pool.queries == []


def test_the_classrooms_teacher_reads_a_student(serve):
    client = serve(FakeIdentity("teacher"), FakePool([evidence("s2", True)]))
    with as_caller(app, token_claims("teacher", sub="t1", org="org")):
        result = client.get("/v1/performance/s2", params={"classroom_id": ROOM}, headers=AUTH)
    assert result.status_code == 200 and result.json()["topics"][0]["correct"] == 1


@pytest.mark.parametrize(
    ("identity", "status"), [(FakeIdentity(None), 403), (FakeIdentity("teacher", down=True), 503)]
)
def test_a_student_read_needs_identitys_word(serve, identity, status):
    pool = FakePool()
    client = serve(identity, pool)
    with as_caller(app, token_claims("teacher", sub="t1", org="org")):
        assert (
            client.get("/v1/performance/s2", params={"classroom_id": ROOM}, headers=AUTH).status_code
            == status
        )
    assert pool.queries == []


def test_ready_needs_the_database(serve):
    class Down(FakePool):
        def execute(self, sql: str, params: Any = None) -> "FakePool":
            raise OSError("down")

    assert serve(FakeIdentity(None), FakePool()).get("/health/ready").json() == {"status": "ready"}
    assert serve(FakeIdentity(None), Down()).get("/health/ready").status_code == 503


def test_shutdown_waits_for_the_consumer_without_blocking_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    import time
    from types import SimpleNamespace

    import main

    for name in (
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "OIDC_JWKS_URL",
        "IDENTITY_URL",
        "KAFKA_BROKERS",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(name, "http://test")
    closed: list[bool] = []
    pool = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(main, "open_pool", lambda url, migrations: pool)

    def consumer(_: Any, stop: threading.Event, connected: threading.Event) -> None:
        stop.wait()
        time.sleep(0.3)  # finishing the message in hand

    monkeypatch.setattr(main, "run", consumer)

    async def scenario() -> int:
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticking = asyncio.create_task(tick())
        async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass
        ticking.cancel()
        return ticks

    assert asyncio.run(scenario()) >= 10  # the loop kept running while the consumer finished
    assert closed == [True]  # and the pool closed only after it
