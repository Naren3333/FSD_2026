"""record every paid model call and enforce the billed user's daily allowance."""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from learning_transport import Contract, failure
from learning_transport.db import AsyncPooled, lock_async
from pydantic import Field

from settings import get_settings

log = logging.getLogger("ai-core.spend")

MIGRATIONS = Path(__file__).parent / "migrations"
Role = Literal["student", "teacher"]

# usd per million tokens: input, cached input, output.
DEFAULT_PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "text-embedding-3-small": (0.02, 0.02, 0.0),
    "rerank-3": (0.05, 0.05, 0.0),
}


class Billed(Contract):
    """the user a call is made for."""

    user_id: str = Field(min_length=1, max_length=200)
    org_id: str = Field(min_length=1, max_length=200)
    role: Role


class Usage(Contract):
    spent_usd: float = Field(ge=0)
    limit_usd: float = Field(gt=0)
    resets_at: datetime


@dataclass(frozen=True)
class Charge:
    operation: str  # embed, generate, stream, rerank, ingest
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    estimated: bool = (
        False  # token counts guessed from text length (a stream cut off before its usage arrived)
    )


def prices() -> dict[str, tuple[float, float, float]]:
    return {**DEFAULT_PRICES, **get_settings().model_prices}


def cost(model: str, tokens_in: int, tokens_out: int = 0, cached_in: int = 0) -> float:
    table = prices()
    if model in table:
        rate_in, rate_cached, rate_out = table[model]
    else:
        log.warning("unpriced_model", extra={"model": model})
        rate_in = rate_cached = max(p[0] for p in table.values())
        rate_out = max(p[2] for p in table.values())
    cached_in = min(cached_in, tokens_in)
    return ((tokens_in - cached_in) * rate_in + cached_in * rate_cached + tokens_out * rate_out) / 1_000_000


def estimate_tokens(text: str) -> int:
    """about four characters a token: for a stream that ended without openai's usage numbers."""
    return (len(text) + 3) // 4


def limit(role: Role) -> float:
    settings = get_settings()
    return settings.spend_limit_teacher_usd if role == "teacher" else settings.spend_limit_student_usd


def timezone() -> str:
    return get_settings().spend_timezone


class Ledger(Protocol):
    async def adjust(self, reservation: str, unused: float) -> None: ...
    async def reserve(self, reservation: str, billed: Billed, amount: float, concurrency: int) -> None: ...

    async def release(self, reservation: str) -> None: ...

    async def record_reserved(
        self, reservation: str, billed: Billed, charge: Charge, correlation: str
    ) -> None: ...

    async def today(self, billed: Billed) -> Usage: ...

    async def record(self, billed: Billed, charge: Charge, correlation_id: str) -> None: ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class PostgresLedger(AsyncPooled):
    MIGRATIONS = MIGRATIONS

    async def metrics(self) -> dict[str, float]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT count(*) AS calls,coalesce(sum(cost_usd),0) AS cost,
            coalesce(sum(tokens_in),0) AS inputs,coalesce(sum(tokens_out),0) AS outputs
            FROM usage WHERE created_at >= date_trunc('day',now() AT TIME ZONE %s) AT TIME ZONE %s""",
                (timezone(), timezone()),
            )
            usage = await cur.fetchone()
            cur = await conn.execute(
                "SELECT count(*) AS reservations,coalesce(sum(amount),0) AS reserved,count(*) FILTER(WHERE expires_at<=now()) AS expired FROM budget_reservations"
            )
            reservations = await cur.fetchone()
        assert usage is not None and reservations is not None
        return {"learning_ai_today_" + key: float(value) for key, value in usage.items()} | {
            "learning_ai_" + key: float(value) for key, value in reservations.items()
        }

    async def reserve(self, reservation: str, billed: Billed, amount: float, concurrency: int) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            total_limit = get_settings().total_spend_limit_usd
            if total_limit is not None:
                # lock before the per-user lock so users and replicas cannot spend the same global
                # allowance.
                await lock_async(conn, "ai-budget", "all-users")
                total = await (
                    await conn.execute(
                        """SELECT (SELECT coalesce(sum(cost_usd),0) FROM usage)
                        +(SELECT coalesce(sum(amount),0) FROM budget_reservations) AS committed"""
                    )
                ).fetchone()
                assert total is not None
                if float(total["committed"]) + amount > total_limit:
                    raise failure(
                        429, "AI_TOTAL_SPEND_LIMIT", "The shared AI spend ceiling has been reached."
                    )
            await lock_async(conn, "ai-budget", billed.org_id + ":" + billed.user_id)
            cur = await conn.execute(
                """SELECT
              (SELECT coalesce(sum(cost_usd),0) FROM usage WHERE org_id=%s AND user_id=%s
                AND created_at >= date_trunc('day',now() AT TIME ZONE %s) AT TIME ZONE %s) AS spent,
              coalesce(sum(amount),0) AS reserved,
              count(*) FILTER (WHERE expires_at>now() AND id<>%s::uuid) AS running,
              (date_trunc('day',now() AT TIME ZONE %s)+interval '1 day') AT TIME ZONE %s AS resets_at
            FROM budget_reservations WHERE org_id=%s AND user_id=%s
              AND (expires_at>now() OR created_at>=date_trunc('day',now() AT TIME ZONE %s) AT TIME ZONE %s)""",
                (
                    billed.org_id,
                    billed.user_id,
                    timezone(),
                    timezone(),
                    reservation,
                    timezone(),
                    timezone(),
                    billed.org_id,
                    billed.user_id,
                    timezone(),
                    timezone(),
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            if row["running"] >= concurrency:
                raise failure(429, "AI_BUSY", "Too many AI requests are running for you at once.")
            if float(row["spent"]) + float(row["reserved"]) + amount > limit(billed.role):
                raise limit_reached(
                    Usage(
                        spent_usd=float(row["spent"]),
                        limit_usd=limit(billed.role),
                        resets_at=row["resets_at"],
                    )
                )
            await conn.execute(
                """INSERT INTO budget_reservations(id,org_id,user_id,amount) VALUES(%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET amount=budget_reservations.amount+excluded.amount,
            expires_at=now()+interval '30 minutes'""",
                (reservation, billed.org_id, billed.user_id, amount),
            )
            # old abandoned reservations are no longer counted after their budget day and lease have passed.
            await conn.execute("DELETE FROM budget_reservations WHERE expires_at<now()-interval '2 days'")

    async def release(self, reservation: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM budget_reservations WHERE id=%s", (reservation,))

    async def adjust(self, reservation: str, unused: float) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE budget_reservations SET amount=greatest(0,amount-%s) WHERE id=%s",
                (unused, reservation),
            )

    async def record_reserved(
        self, reservation: str, billed: Billed, charge: Charge, correlation: str
    ) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO usage(org_id,user_id,role,operation,model,tokens_in,tokens_out,cost_usd,estimated,correlation_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    billed.org_id,
                    billed.user_id,
                    billed.role,
                    charge.operation,
                    charge.model,
                    charge.tokens_in,
                    charge.tokens_out,
                    charge.cost_usd,
                    charge.estimated,
                    correlation,
                ),
            )
            await conn.execute(
                "UPDATE budget_reservations SET amount=greatest(0,amount-%s) WHERE id=%s",
                (charge.cost_usd, reservation),
            )

    async def today(self, billed: Billed) -> Usage:
        # compute allowance boundaries from local calendar dates in the configured database time zone.
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """WITH local AS (SELECT date_trunc('day', now() AT TIME ZONE %(tz)s) AS midnight),
                day AS (SELECT midnight AT TIME ZONE %(tz)s AS start,
                  (midnight + interval '1 day') AT TIME ZONE %(tz)s AS resets_at FROM local)
                SELECT coalesce(sum(u.cost_usd), 0)::float8 AS spent, day.resets_at
                FROM day LEFT JOIN usage u ON u.org_id=%(org)s AND u.user_id=%(user)s AND u.created_at>=day.start
                GROUP BY day.start, day.resets_at""",
                {"tz": timezone(), "org": billed.org_id, "user": billed.user_id},
            )
            row = await cur.fetchone()
        assert row is not None
        return Usage(
            spent_usd=round(row["spent"], 6),
            limit_usd=limit(billed.role),
            resets_at=row["resets_at"].astimezone(ZoneInfo(timezone())),
        )

    async def record(self, billed: Billed, charge: Charge, correlation_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO usage(org_id,user_id,role,operation,model,tokens_in,tokens_out,cost_usd,estimated,
                correlation_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    billed.org_id,
                    billed.user_id,
                    billed.role,
                    charge.operation,
                    charge.model,
                    charge.tokens_in,
                    charge.tokens_out,
                    charge.cost_usd,
                    charge.estimated,
                    correlation_id,
                ),
            )


def over_limit(usage: Usage) -> bool:
    return usage.spent_usd >= usage.limit_usd


def limit_reached(usage: Usage) -> Exception:
    """429 with the numbers, so the caller can say when the allowance comes back."""
    error = failure(
        429,
        "AI_SPEND_LIMIT",
        f"Today's AI allowance of ${usage.limit_usd:.2f} is used up. It resets at {usage.resets_at.isoformat()}.",
    )
    assert isinstance(error.detail, dict)
    error.detail.update(
        spent_usd=usage.spent_usd, limit_usd=usage.limit_usd, resets_at=usage.resets_at.isoformat()
    )
    return error
