"""read-only reports from ai-core's own spend ledger."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from zoneinfo import ZoneInfo

from learning_transport import Contract
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from settings import get_settings
from spend import PostgresLedger, timezone

Period = Literal["today", "last_7_days"]
ZERO = Decimal(0)
AMOUNTS = "sum(cost_usd) AS cost, sum(cost_usd) FILTER (WHERE estimated) AS estimated, count(*) AS calls"


class SpendAmounts(Contract):
    recorded_usd: str
    estimated_usd: str
    calls: int


class DailySpend(SpendAmounts):
    day: date


class OperationSpend(SpendAmounts):
    operation: str


class Reservations(Contract):
    count: int
    reserved_usd: str
    expired_count: int
    expired_usd: str


class Ceiling(Contract):
    limit_usd: str
    remaining_usd: str


class SpendSummary(Contract):
    as_of: datetime
    timezone: str
    today: DailySpend
    days: list[DailySpend]
    operations: list[OperationSpend]
    reservations: Reservations
    ceiling: Ceiling | None


class UserSpend(SpendAmounts):
    org_id: str
    user_id: str


class UserSpendPage(Contract):
    as_of: datetime
    timezone: str
    total: int
    items: list[UserSpend]


def dollars(value: Decimal) -> str:
    return format(value, "f")


def amounts(row: DictRow) -> SpendAmounts:
    return SpendAmounts(
        recorded_usd=dollars(row["cost"] or ZERO),
        estimated_usd=dollars(row["estimated"] or ZERO),
        calls=row["calls"],
    )


async def report_clock(conn: AsyncConnection[DictRow]) -> datetime:
    row = await (await conn.execute("SELECT now() AS at")).fetchone()
    assert row is not None
    return cast(datetime, row["at"]).astimezone(ZoneInfo(timezone()))


async def summary(ledger: PostgresLedger, org_id: str | None = None) -> SpendSummary:
    """the last seven days' spend and the held reservations, of one organization when org_id is given. the
    platform-wide ceiling covers every organization, so an organization's report leaves it out."""
    async with ledger.pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        at = await report_clock(conn)
        today = at.date()
        first = today - timedelta(days=6)
        rows = await (
            await conn.execute(
                f"""WITH recent AS (
                     SELECT (created_at AT TIME ZONE %(zone)s)::date AS day, operation, cost_usd, estimated
                     FROM usage WHERE created_at >= (%(start)s::timestamp AT TIME ZONE %(zone)s)
                       AND created_at <= %(at)s AND (%(org)s::text IS NULL OR org_id = %(org)s)
                   ) SELECT day, operation, {AMOUNTS} FROM recent
                   GROUP BY GROUPING SETS ((day), (day, operation)) ORDER BY day, operation""",
                {"zone": timezone(), "start": first, "at": at, "org": org_id},
            )
        ).fetchall()
        limit = get_settings().total_spend_limit_usd if org_id is None else None
        held = await (
            await conn.execute(
                """SELECT count(*) AS count, coalesce(sum(amount),0) AS reserved,
                   count(*) FILTER (WHERE expires_at<=%(at)s) AS expired_count,
                   coalesce(sum(amount) FILTER (WHERE expires_at<=%(at)s),0) AS expired,
                   CASE WHEN %(limited)s THEN
                       (SELECT sum(cost_usd) FROM usage_lifetime_total) END AS spent
                   FROM budget_reservations WHERE %(org)s::text IS NULL OR org_id = %(org)s""",
                {"at": at, "limited": limit is not None, "org": org_id},
            )
        ).fetchone()
    assert held is not None
    daily = {row["day"]: amounts(row) for row in rows if row["operation"] is None}
    empty = SpendAmounts(recorded_usd="0", estimated_usd="0", calls=0)
    days = [
        DailySpend(day=day, **daily.get(day, empty).model_dump())
        for day in (first + timedelta(days=i) for i in range(7))
    ]
    ceiling = None
    if limit is not None:
        maximum = Decimal(str(limit))
        ceiling = Ceiling(
            limit_usd=dollars(maximum),
            remaining_usd=dollars(max(ZERO, maximum - held["spent"] - held["reserved"])),
        )
    return SpendSummary(
        as_of=at,
        timezone=timezone(),
        today=days[-1],
        days=days,
        operations=[
            OperationSpend(operation=row["operation"], **amounts(row).model_dump())
            for row in rows
            if row["day"] == today and row["operation"] is not None
        ],
        reservations=Reservations(
            count=held["count"],
            reserved_usd=dollars(held["reserved"]),
            expired_count=held["expired_count"],
            expired_usd=dollars(held["expired"]),
        ),
        ceiling=ceiling,
    )


async def users(
    ledger: PostgresLedger, period: Period, org_id: str | None, user_id: str | None, offset: int
) -> UserSpendPage:
    async with ledger.pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ ONLY")
        at = await report_clock(conn)
        first = at.date() - timedelta(days=6 if period == "last_7_days" else 0)
        rows = await (
            await conn.execute(
                f"""WITH accounts AS (
                   SELECT org_id, user_id, {AMOUNTS} FROM usage
                   WHERE created_at >= (%(start)s::timestamp AT TIME ZONE %(zone)s) AND created_at <= %(at)s
                     AND (%(org)s::text IS NULL OR org_id = %(org)s)
                     AND (%(user)s::text IS NULL OR user_id = %(user)s)
                   GROUP BY org_id, user_id
                 ), page AS (
                   SELECT * FROM accounts ORDER BY cost DESC, org_id, user_id LIMIT 20 OFFSET %(offset)s
                 ) SELECT total, page.* FROM (SELECT count(*) AS total FROM accounts) AS count
                 LEFT JOIN page ON true ORDER BY cost DESC, org_id, user_id""",
                {
                    "zone": timezone(),
                    "start": first,
                    "at": at,
                    "org": org_id,
                    "user": user_id,
                    "offset": offset,
                },
            )
        ).fetchall()
    return UserSpendPage(
        as_of=at,
        timezone=timezone(),
        total=rows[0]["total"],
        items=[
            UserSpend(org_id=row["org_id"], user_id=row["user_id"], **amounts(row).model_dump())
            for row in rows
            if row["user_id"] is not None
        ],
    )
