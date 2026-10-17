"""PostgresLedger against a real PostgreSQL: the day a user's spend is summed over starts at local midnight in
SPEND_TIMEZONE, and resets_at is the next local midnight, across DST changes too (ADR 0003). Needs
AICORE_TEST_DATABASE_URL, a scratch database; each test migrates and then drops its own schema. Skipped without it.

The clock is pinned without touching spend.py: the test schema comes before pg_catalog on the search_path and
holds its own now(), which reads a one-row `clock` table (the real now() when it is empty)."""

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from conftest import configured
from learning_transport import db

import spend

pytestmark = pytest.mark.anyio
URL = os.environ.get("AICORE_TEST_DATABASE_URL")
SGT = ZoneInfo("Asia/Singapore")
LONDON = ZoneInfo("Europe/London")
CLOCK = """CREATE TABLE clock(at timestamptz NOT NULL);
CREATE FUNCTION now() RETURNS timestamptz LANGUAGE sql STABLE
  AS $$ SELECT coalesce((SELECT at FROM clock), pg_catalog.now()) $$;"""


class Pinned:
    """A ledger in its own schema, with a clock the test sets."""

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
async def pinned(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Pinned]:
    if not URL:
        pytest.skip("AICORE_TEST_DATABASE_URL is not set")
    schema = f"ledger_{uuid4().hex}"
    options = f"-c statement_timeout=10000 -c search_path={schema},pg_catalog"
    monkeypatch.setattr(db, "CONNECTION_OPTIONS", {**db.CONNECTION_OPTIONS, "options": options})
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA {schema}")
    ledger = await spend.PostgresLedger.open(URL)  # migrates into the new schema
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


async def test_the_day_starts_at_local_midnight(pinned: Pinned) -> None:
    user = str(uuid4())
    await pinned.at(datetime(2026, 10, 17, 7, 30, tzinfo=SGT))  # still the 16th in UTC
    await pinned.spent(user, "org-a", 1.0, datetime(2026, 10, 16, 23, 59, tzinfo=SGT))  # yesterday
    await pinned.spent(user, "org-a", 0.25, datetime(2026, 10, 17, 0, 1, tzinfo=SGT))
    usage = await pinned.ledger.today(billed(user))
    assert usage.spent_usd == 0.25 and usage.limit_usd == 5.0
    assert usage.resets_at == datetime(2026, 10, 18, tzinfo=SGT)
    assert usage.resets_at.utcoffset() == SGT.utcoffset(usage.resets_at)


async def test_recorded_charges_count_today_however_small(pinned: Pinned) -> None:
    """Rows keep 10 decimal places, so three sub-cent-fraction embeddings still add up."""
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
        # Clocks go back at 02:00 BST on 25 October 2026: a 25-hour day.
        ("2026-10-25T12:00:00+00:00", "2026-10-24T23:00:00+00:00", "2026-10-26T00:00:00+00:00"),
        # Clocks go forward at 01:00 GMT on 29 March 2026: a 23-hour day.
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
