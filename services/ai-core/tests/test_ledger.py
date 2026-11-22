"""verify allowance resets at local midnight, including daylight-saving changes."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from conftest import configured
from fastapi import HTTPException
from learning_transport import db

import spend
import spend_report

pytestmark = pytest.mark.anyio
URL = os.environ.get("AICORE_TEST_DATABASE_URL")
SGT = ZoneInfo("Asia/Singapore")
LONDON = ZoneInfo("Europe/London")
CLOCK = """CREATE TABLE clock(at timestamptz NOT NULL);
CREATE FUNCTION now() RETURNS timestamptz LANGUAGE sql STABLE
  AS $$ SELECT coalesce((SELECT at FROM clock), pg_catalog.now()) $$;"""


class Pinned:
    """a ledger in its own schema, with a clock the test sets."""

    def __init__(self, ledger: spend.PostgresLedger) -> None:
        self.ledger = ledger

    async def at(self, moment: datetime) -> None:
        async with self.ledger.pool.connection() as conn:
            await conn.execute("DELETE FROM clock")
            await conn.execute("INSERT INTO clock(at) VALUES(%s)", (moment,))

    async def spent(self, user: str, org: str, cost: float, at: datetime) -> None:
        async with self.ledger.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO usage(org_id,user_id,role,operation,model,tokens_in,tokens_out,cost_usd,
                correlation_id,created_at) VALUES(%s,%s,'student','embed','m',1,0,%s,'t',%s)""",
                (org, user, cost, at),
            )


@pytest.fixture
async def pinned(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Pinned]:
    if not URL:
        pytest.skip("AICORE_TEST_DATABASE_URL is not set")
    schema = f"ledger_{uuid4().hex}"
    options = f"-c statement_timeout=10000 -c search_path={schema},pg_catalog"
    monkeypatch.setattr(db, "CONNECTION_OPTIONS", {**db.CONNECTION_OPTIONS, "options": options})
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA {schema}")
    ledger_type = spend.PostgresLedger
    if legacy_version := getattr(request, "param", None):
        for step in db.migrations(spend.MIGRATIONS)[:legacy_version]:
            (tmp_path / step.name).write_text(step.sql, encoding="utf-8")

        class LegacyLedger(spend.PostgresLedger):
            MIGRATIONS = tmp_path

        ledger_type = LegacyLedger
    ledger = await ledger_type.open(URL)
    try:
        async with ledger.pool.connection() as conn:
            await conn.execute(CLOCK)
        yield Pinned(ledger)
    finally:
        await ledger.close()
        async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
            await conn.execute(f"DROP SCHEMA {schema} CASCADE")


def billed(user: str, org: str = "org-a", role: spend.Role = "student") -> spend.Billed:
    return spend.Billed(user_id=user, org_id=org, role=role)


async def test_concurrent_reservations_cannot_both_spend_the_same_allowance(pinned: Pinned) -> None:
    who = billed(str(uuid4()))
    ids = [str(uuid4()), str(uuid4())]
    outcomes = await asyncio.gather(
        *(pinned.ledger.reserve(i, who, 3.0, 10) for i in ids), return_exceptions=True
    )
    assert sum(o is None for o in outcomes) == 1
    assert sum(isinstance(o, HTTPException) and o.status_code == 429 for o in outcomes) == 1
    for i in ids:
        await pinned.ledger.release(i)
    await pinned.ledger.reserve(str(uuid4()), who, 4.0, 10)


async def test_date_index_migration_allows_concurrent_starts_and_writes(pinned: Pinned) -> None:
    assert URL is not None
    at = datetime(2026, 11, 18, tzinfo=SGT)
    async with pinned.ledger.pool.connection() as conn:
        await conn.execute("DROP INDEX usage_by_date")
        await conn.execute("DELETE FROM schema_migrations WHERE version = 4")

    async def apply(url: str) -> None:
        ledger = await spend.PostgresLedger.open(url)
        await ledger.close()

    waiting = """SELECT 1 FROM pg_stat_progress_create_index
                 WHERE command = 'CREATE INDEX CONCURRENTLY'
                   AND phase = 'waiting for writers before build'"""
    second = psycopg.conninfo.make_conninfo(URL, application_name="test_second_ledger_migration")
    competing = """SELECT 1 FROM pg_stat_activity
                   WHERE application_name = 'test_second_ledger_migration'
                     AND query ~ 'pg_(try_)?advisory_lock'"""
    async with (
        asyncio.timeout(20),
        await psycopg.AsyncConnection.connect(URL, autocommit=True) as observer,
        asyncio.TaskGroup() as group,
        pinned.ledger.pool.connection() as blocker,
    ):
        await blocker.execute("UPDATE usage SET created_at = created_at")
        group.create_task(apply(URL))
        async with asyncio.timeout(8):
            while not await (await observer.execute(waiting)).fetchone():
                await asyncio.sleep(0.05)
            group.create_task(apply(second))
            while not await (await observer.execute(competing)).fetchone():
                await asyncio.sleep(0.05)
        await asyncio.wait_for(pinned.spent("new", "school", 0.1, at), timeout=3)
    async with pinned.ledger.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = 'usage_by_date'::regclass"
        )
        assert await cur.fetchone() == {"indisvalid": True}
        cur = await conn.execute("SELECT version FROM schema_migrations WHERE version = 4")
        assert await cur.fetchone() == {"version": 4}


async def test_date_index_migration_recovers_an_invalid_index(pinned: Pinned) -> None:
    assert URL is not None
    at = datetime(2026, 11, 18, tzinfo=SGT)
    await pinned.spent("one", "school", 0.1, at)
    await pinned.spent("two", "school", 0.1, at)
    async with pinned.ledger.pool.connection() as conn:
        await conn.execute("DROP INDEX usage_by_date")
        await conn.execute("DELETE FROM schema_migrations WHERE version = 4")
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True, **db.CONNECTION_OPTIONS) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute("CREATE UNIQUE INDEX CONCURRENTLY usage_by_date ON usage (created_at)")
    ledger = await spend.PostgresLedger.open(URL)
    try:
        async with ledger.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT indisvalid, indisunique FROM pg_index WHERE indexrelid = 'usage_by_date'::regclass"
            )
            assert await cur.fetchone() == {"indisvalid": True, "indisunique": False}
            cur = await conn.execute("SELECT count(*) AS total FROM usage")
            assert await cur.fetchone() == {"total": 2}
    finally:
        await ledger.close()


@pytest.mark.parametrize("pinned", [4], indirect=True)
async def test_lifetime_total_migration_yields_to_writers_until_it_gets_its_lock(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert URL is not None
    monkeypatch.setattr(db, "MIGRATION_LOCK_WAIT", 0.5)
    at = datetime(2026, 11, 18, tzinfo=SGT)

    async def trigger_lock_waiting() -> bool:
        async with pinned.ledger.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT count(*) AS waiting FROM pg_locks WHERE relation = 'usage'::regclass
                   AND mode = 'ShareRowExclusiveLock' AND NOT granted"""
            )
            row = await cur.fetchone()
            return bool(row and row["waiting"])

    async def until(waiting: bool) -> None:
        while await trigger_lock_waiting() != waiting:
            await asyncio.sleep(0.02)

    async with pinned.ledger.pool.connection() as blocker:
        # an open ledger write keeps the trigger's table lock out of reach.
        await blocker.execute("UPDATE usage SET created_at = created_at")
        upgrade = asyncio.create_task(spend.PostgresLedger.open(URL))
        async with asyncio.timeout(10):
            # the migration asks for the lock, gives up, and asks again.
            await until(True)
            await until(False)
            await until(True)
        assert not upgrade.done()
        # while the blocker still holds the table, a writer queued behind the request is let through.
        await asyncio.wait_for(pinned.spent("writer", "school", 0.1, at), timeout=10)
    ledger = await asyncio.wait_for(upgrade, timeout=15)
    try:
        async with ledger.pool.connection() as conn:
            cur = await conn.execute("SELECT sum(cost_usd) AS total FROM usage_lifetime_total")
            assert await cur.fetchone() == {"total": Decimal("0.1")}
    finally:
        await ledger.close()


@pytest.mark.parametrize("pinned", [3], indirect=True)
async def test_a_starting_replica_waits_out_another_replicas_long_migration(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert URL is not None
    # the pool's own statement limit is shorter than the other replica's migration.
    options = db.CONNECTION_OPTIONS["options"].replace("statement_timeout=10000", "statement_timeout=300")
    monkeypatch.setattr(db, "CONNECTION_OPTIONS", {**db.CONNECTION_OPTIONS, "options": options})

    class Replica(spend.PostgresLedger):
        MIGRATIONS = tmp_path  # 001-003 only: no concurrent index, so the pool applies them

    async with pinned.ledger.pool.connection() as migrating:
        await migrating.execute(db.LOCK, (db.LOCK_MIGRATIONS, ""))
        replica = asyncio.create_task(Replica.open(URL))
        async with asyncio.timeout(10):
            while not replica.done():
                cur = await migrating.execute(
                    "SELECT count(*) AS waiting FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                )
                row = await cur.fetchone()
                if row and row["waiting"]:
                    break
                await asyncio.sleep(0.02)
        # well past the pool's limit, the replica is still waiting rather than failed.
        await asyncio.sleep(1)
        assert not replica.done()
    ledger = await asyncio.wait_for(replica, timeout=10)
    await ledger.close()


async def test_operator_reports_group_exact_ledger_amounts_at_budget_midnight(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    midnight = datetime(2026, 11, 18, tzinfo=SGT)
    now = midnight + timedelta(minutes=1)
    await pinned.at(now)
    monkeypatch.setattr(spend_report, "get_settings", lambda: configured(total_spend_limit_usd=50))
    async with pinned.ledger.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO usage(org_id,user_id,role,operation,model,tokens_in,tokens_out,cost_usd,
              estimated,correlation_id,created_at) VALUES
              ('org-a','deleted-account','teacher','generate','m',1,1,0.0000000123,true,'t',%s),
              ('org-b','deleted-account','student','embed','m',1,0,0.5,false,'t',%s),
              ('org-a','deleted-account','teacher','embed','m',1,0,1.5,false,'t',%s),
              ('org-a','older-account','student','rerank','m',1,0,2,false,'t',%s),
              ('org-a','future-account','student','embed','m',1,0,3,false,'t',%s)""",
            (
                midnight,
                now,
                midnight - timedelta(microseconds=1),
                midnight - timedelta(days=7),
                now + timedelta(seconds=1),
            ),
        )
        await conn.execute(
            """INSERT INTO budget_reservations(id,org_id,user_id,amount,expires_at)
               VALUES (%s,'org-a','deleted-account',1,%s), (%s,'org-b','deleted-account',2,%s)""",
            (uuid4(), now, uuid4(), now + timedelta(minutes=1)),
        )

    report = await spend_report.summary(pinned.ledger)
    assert report.as_of == now and report.timezone == "Asia/Singapore"
    assert report.today.calls == 2 and report.today.recorded_usd == "0.5000000123"
    assert report.today.estimated_usd == "0.0000000123"
    assert len(report.days) == 7 and report.days[-2].recorded_usd == "1.5000000000"
    assert [row.operation for row in report.operations] == ["embed", "generate"]
    assert report.reservations.count == 2 and report.reservations.expired_count == 1
    assert Decimal(report.reservations.reserved_usd) == 3 and Decimal(report.reservations.expired_usd) == 1
    assert report.ceiling and report.ceiling.remaining_usd == "39.9999999877"
    # one school's report counts only its own spend and holds, and leaves out the platform-wide ceiling.
    school = await spend_report.summary(pinned.ledger, "org-b")
    assert school.today.calls == 1 and school.today.recorded_usd == "0.5000000000"
    assert school.days[-2].calls == 0 and [row.operation for row in school.operations] == ["embed"]
    assert school.reservations.count == 1 and Decimal(school.reservations.reserved_usd) == 2
    assert school.ceiling is None

    todays_users = await spend_report.users(pinned.ledger, "today", None, "deleted-account", 0)
    assert todays_users.total == 2
    assert [user.org_id for user in todays_users.items] == ["org-b", "org-a"]
    week = await spend_report.users(pinned.ledger, "last_7_days", "org-a", None, 0)
    assert week.total == 1 and week.items[0].recorded_usd == "1.5000000123"


async def test_operator_spend_pages_have_stable_ties_and_totals(pinned: Pinned) -> None:
    now = datetime(2026, 11, 18, 12, tzinfo=SGT)
    await pinned.at(now)
    async with pinned.ledger.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO usage(org_id,user_id,role,operation,model,tokens_in,tokens_out,cost_usd,
              correlation_id,created_at)
              SELECT 'org-a','account-' || lpad(i::text, 2, '0'),'student','embed','m',1,0,1,'t',%s
              FROM generate_series(1,22) AS i""",
            (now,),
        )
    first = await spend_report.users(pinned.ledger, "today", None, None, 0)
    next_page = await spend_report.users(pinned.ledger, "today", None, None, 20)
    last = await spend_report.users(pinned.ledger, "today", None, None, 40)
    assert first.total == next_page.total == last.total == 22
    assert [row.user_id for row in first.items] == [f"account-{i:02d}" for i in range(1, 21)]
    assert [row.user_id for row in next_page.items] == ["account-21", "account-22"]
    assert last.items == []


@pytest.mark.parametrize("pinned", [4], indirect=True)
async def test_lifetime_total_backfills_and_tracks_ledger_transactions(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert URL is not None
    settings = configured(total_spend_limit_usd=5.0)
    monkeypatch.setattr(spend, "get_settings", lambda: settings)
    monkeypatch.setattr(spend_report, "get_settings", lambda: settings)
    prior = datetime(2026, 11, 1, tzinfo=SGT)
    await pinned.at(prior + timedelta(days=17))
    await pinned.spent("existing", "school", 1.0000000001, prior)
    ledger = await spend.PostgresLedger.open(URL)
    try:
        accrued = await spend_report.summary(ledger)
        assert accrued.ceiling is not None and accrued.ceiling.remaining_usd == "3.9999999999"
        await asyncio.gather(
            pinned.spent("older-writer", "school", 0.5, prior),
            ledger.record(billed("new-writer"), spend.Charge("embed", "m", 1, 0, 1.5), "new"),
        )
        async with ledger.pool.connection() as conn:
            await conn.execute("UPDATE usage SET cost_usd = cost_usd + 0.25 WHERE user_id = 'existing'")
            await conn.execute("DELETE FROM usage WHERE user_id = 'older-writer'")
            async with conn.transaction(force_rollback=True):
                await conn.execute("UPDATE usage SET cost_usd = 0")

        report = await spend_report.summary(ledger)
        assert report.ceiling is not None and report.ceiling.remaining_usd == "2.2499999999"
        with pytest.raises(HTTPException) as exc:
            await ledger.reserve(str(uuid4()), billed("next"), 2.3, 10)
        assert exc.value.detail["code"] == "AI_TOTAL_SPEND_LIMIT"
        await ledger.reserve(str(uuid4()), billed("next"), 2.2, 10)
    finally:
        await ledger.close()


@pytest.mark.parametrize("pinned", [4], indirect=True)
async def test_lifetime_backfill_counts_rows_changed_between_trigger_and_backfill(
    pinned: Pinned, tmp_path: Path
) -> None:
    assert URL is not None
    at = datetime(2026, 11, 18, tzinfo=SGT)
    for user, cost in (("kept", 1), ("changed", 2), ("removed", 4)):
        await pinned.spent(user, "school", cost, at)
    triggered = tmp_path / "triggered"
    triggered.mkdir()
    for step in db.migrations(spend.MIGRATIONS)[:5]:
        (triggered / step.name).write_text(step.sql, encoding="utf-8")

    class Triggered(spend.PostgresLedger):
        MIGRATIONS = triggered  # the trigger is in place, the backfill has not run

    await (await Triggered.open(URL)).close()
    async with pinned.ledger.pool.connection() as conn:
        await conn.execute("UPDATE usage SET cost_usd = 3 WHERE user_id = 'changed'")
        await conn.execute("DELETE FROM usage WHERE user_id = 'removed'")
    await pinned.spent("new", "school", 8, at)
    async with pinned.ledger.pool.connection() as conn:
        await conn.execute("UPDATE usage SET cost_usd = 16 WHERE user_id = 'new'")

    async def lifetime(ledger: spend.PostgresLedger) -> Decimal:
        async with ledger.pool.connection() as conn:
            cur = await conn.execute("SELECT sum(cost_usd) AS total FROM usage_lifetime_total")
            row = await cur.fetchone()
            assert row is not None
            return Decimal(row["total"])

    ledger = await spend.PostgresLedger.open(URL)
    try:
        assert await lifetime(ledger) == 20
        async with ledger.pool.connection() as conn:
            await conn.execute("UPDATE usage SET cost_usd = 5 WHERE user_id = 'kept'")
            cur = await conn.execute("SELECT to_regclass('usage_lifetime_backfill') IS NULL AS done")
            assert await cur.fetchone() == {"done": True}
        assert await lifetime(ledger) == 24
    finally:
        await ledger.close()


async def test_shared_concurrency_and_organisation_isolation(pinned: Pinned) -> None:
    who = billed(str(uuid4()))
    first, second = str(uuid4()), str(uuid4())
    await pinned.ledger.reserve(first, who, 1.0, 1)
    with pytest.raises(HTTPException) as exc:
        await pinned.ledger.reserve(second, who, 0, 1)
    assert exc.value.detail["code"] == "AI_BUSY"
    await pinned.ledger.reserve(second, billed(who.user_id, "another-org"), 1.0, 1)
    await pinned.ledger.release(first)
    await pinned.ledger.reserve(str(uuid4()), who, 1.0, 1)


async def test_total_ceiling_is_shared_by_different_users_and_keeps_recorded_usage(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = configured(total_spend_limit_usd=4.0)
    monkeypatch.setattr("spend.get_settings", lambda: settings)
    first, second = billed(str(uuid4())), billed(str(uuid4()), "org-b")
    ids = str(uuid4()), str(uuid4())
    results = await asyncio.gather(
        pinned.ledger.reserve(ids[0], first, 3.0, 10),
        pinned.ledger.reserve(ids[1], second, 3.0, 10),
        return_exceptions=True,
    )
    assert sum(r is None for r in results) == 1
    [blocked] = [r for r in results if isinstance(r, HTTPException)]
    assert blocked.detail["code"] == "AI_TOTAL_SPEND_LIMIT"
    winner = 0 if results[0] is None else 1
    await pinned.ledger.record_reserved(
        ids[winner], (first, second)[winner], spend.Charge("embed", "m", 1, 0, 3.0), "test"
    )
    await pinned.ledger.release(ids[winner])
    with pytest.raises(HTTPException) as exc:
        await pinned.ledger.reserve(str(uuid4()), billed(str(uuid4())), 2.0, 10)
    assert exc.value.detail["code"] == "AI_TOTAL_SPEND_LIMIT"


async def test_usage_and_reservation_settle_in_one_transaction(pinned: Pinned) -> None:
    who = billed(str(uuid4()))
    reservation = str(uuid4())
    await pinned.ledger.reserve(reservation, who, 4.0, 10)
    await pinned.ledger.record_reserved(reservation, who, spend.Charge("embed", "m", 1, 0, 1.0), "test")
    async with pinned.ledger.pool.connection() as conn:
        row = await (
            await conn.execute("SELECT amount FROM budget_reservations WHERE id=%s", (reservation,))
        ).fetchone()
    assert row and float(row["amount"]) == 3.0
    assert (await pinned.ledger.today(who)).spent_usd == 1.0
    await pinned.ledger.release(reservation)
    assert (await pinned.ledger.today(who)).spent_usd == 1.0


async def test_the_day_starts_at_local_midnight(pinned: Pinned) -> None:
    user = str(uuid4())
    await pinned.at(datetime(2026, 10, 17, 7, 30, tzinfo=SGT))  # still the 16th in utc
    await pinned.spent(user, "org-a", 1.0, datetime(2026, 10, 16, 23, 59, tzinfo=SGT))  # yesterday
    await pinned.spent(user, "org-a", 0.25, datetime(2026, 10, 17, 0, 1, tzinfo=SGT))
    usage = await pinned.ledger.today(billed(user))
    assert usage.spent_usd == 0.25 and usage.limit_usd == 5.0
    assert usage.resets_at == datetime(2026, 10, 18, tzinfo=SGT)
    assert usage.resets_at.utcoffset() == SGT.utcoffset(usage.resets_at)


async def test_recorded_charges_count_today_however_small(pinned: Pinned) -> None:
    """rows keep 10 decimal places, so three sub-cent-fraction embeddings still add up."""
    user = str(uuid4())
    charge = spend.Charge("embed", "text-embedding-3-small", 20, 0, 0.0000004)
    for _ in range(3):
        await pinned.ledger.record(billed(user), charge, "corr")
    assert (await pinned.ledger.today(billed(user))).spent_usd == 0.000001


async def test_the_same_user_id_in_another_organization_is_someone_else(pinned: Pinned) -> None:
    user = str(uuid4())
    await pinned.at(datetime(2026, 10, 17, 12, 0, tzinfo=SGT))
    await pinned.spent(user, "org-a", 2.0, datetime(2026, 10, 17, 9, 0, tzinfo=SGT))
    await pinned.spent(user, "org-b", 4.5, datetime(2026, 10, 17, 9, 0, tzinfo=SGT))
    assert (await pinned.ledger.today(billed(user, "org-a"))).spent_usd == 2.0
    assert (await pinned.ledger.today(billed(user, "org-b", "teacher"))).spent_usd == 4.5
    assert (await pinned.ledger.today(billed(user, "org-c"))).spent_usd == 0.0


@pytest.mark.parametrize(
    ("noon", "midnight", "resets_at"),
    [
        # clocks go back at 02:00 bst on 25 october 2026: a 25-hour day.
        ("2026-10-25T12:00:00+00:00", "2026-10-24T23:00:00+00:00", "2026-10-26T00:00:00+00:00"),
        # clocks go forward at 01:00 gmt on 29 march 2026: a 23-hour day.
        ("2026-03-29T12:00:00+01:00", "2026-03-29T00:00:00+00:00", "2026-03-29T23:00:00+00:00"),
    ],
    ids=["autumn", "spring"],
)
async def test_a_dst_change_day_resets_at_the_next_local_midnight(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch, noon: str, midnight: str, resets_at: str
) -> None:
    london = configured(spend_timezone="Europe/London")
    monkeypatch.setattr("spend.get_settings", lambda: london)
    user = str(uuid4())
    start = datetime.fromisoformat(midnight)
    await pinned.at(datetime.fromisoformat(noon))
    await pinned.spent(user, "org-a", 1.0, start - timedelta(minutes=1))  # 23:59 the day before
    await pinned.spent(user, "org-a", 0.5, start)
    usage = await pinned.ledger.today(billed(user))
    assert usage.spent_usd == 0.5
    assert usage.resets_at == datetime.fromisoformat(resets_at)
    assert usage.resets_at.astimezone(LONDON).hour == 0 and usage.resets_at.tzinfo == LONDON


async def test_a_recorded_call_keeps_its_outcome_and_latency(pinned: Pinned) -> None:
    """migration 007 adds both columns; a charge without them, as ingest writes, leaves them null."""
    user = str(uuid4())
    timed = spend.Charge("generate", "gpt-check", 10, 5, 0.001, outcome="rejected", latency_ms=1234)
    await pinned.ledger.record(billed(user), timed, "timed")
    await pinned.ledger.record(billed(user), spend.Charge("ingest", "m", 1, 0, 0.001), "untimed")
    async with pinned.ledger.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT correlation_id,model,outcome,latency_ms FROM usage WHERE user_id=%s ORDER BY id", (user,)
        )
        rows = await cur.fetchall()
    assert [tuple(r.values()) for r in rows] == [
        ("timed", "gpt-check", "rejected", 1234),
        ("untimed", "m", None, None),
    ]
    with pytest.raises(psycopg.errors.CheckViolation):
        await pinned.ledger.record(billed(user), spend.Charge("embed", "m", 1, 0, 0.0, outcome="lost"), "x")  # type: ignore[arg-type]
