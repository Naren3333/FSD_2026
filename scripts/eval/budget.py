"""a paid eval run's spending cap, measured from ai-core's ledger by each request's correlation id."""

import subprocess
import uuid
from datetime import UTC, datetime, timedelta

from common import ROOT
from local_stack import psql_json

DEFAULT_CAP = 0.50  # usd per run
# what one case of each kind is assumed to cost before any has been measured.
FLOORS = {"performance": 0.05, "generation": 0.05, "probe": 0.02}
DEFAULT_LIMIT = 10.0  # ai-core's teacher allowance in docker-compose.yml


class CapReachedError(Exception):
    """the next case could take the run past its cap."""


def correlation() -> str:
    return str(uuid.uuid4())


def ledger_cost(correlation_id: str) -> float:
    """what ai-core recorded for one request, across every call it made."""
    rows = psql_json(
        f"SELECT coalesce(sum(cost_usd), 0)::float AS cost FROM usage WHERE correlation_id='{uuid.UUID(correlation_id)}'",
        database="aicore",
    )
    return float(rows[0]["cost"]) if rows else 0.0


def ledger_calls(correlation_id: str, operation: str) -> int:
    rows = psql_json(
        f"SELECT count(*) AS n FROM usage WHERE correlation_id='{uuid.UUID(correlation_id)}' AND operation='{operation}'",
        database="aicore",
    )
    return int(rows[0]["n"]) if rows else 0


def spent_since(user_id: str, since: datetime) -> float:
    rows = psql_json(
        "SELECT coalesce(sum(cost_usd), 0)::float AS cost FROM usage "
        f"WHERE user_id='{uuid.UUID(user_id)}' AND created_at >= '{since.isoformat()}'",
        database="aicore",
    )
    return float(rows[0]["cost"]) if rows else 0.0


def teacher_limit() -> float:
    """the teacher allowance the running ai-core enforces, or the compose default when it can't be read."""
    try:
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "ai-core", "printenv", "SPEND_LIMIT_TEACHER_USD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        return float(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return DEFAULT_LIMIT


class Budget:
    def __init__(self, cap: float, teacher_id: str) -> None:
        self.cap, self.teacher_id = cap, teacher_id
        self.spent = 0.0
        self.largest: dict[str, float] = {}

    def preflight(self) -> None:
        """refuses to start when the teacher's allowance left today can't cover the cap. ai-core's day follows its own time zone, so the last 24 hours stand in for it: never less than today's spend."""
        today = spent_since(self.teacher_id, datetime.now(UTC) - timedelta(days=1))
        limit = teacher_limit()
        print(
            f"Eval teacher spent ${today:.4f} in the last 24 hours of a ${limit:.2f} daily allowance; "
            f"this run is capped at ${self.cap:.2f}."
        )
        if limit - today < self.cap:
            raise SystemExit(
                f"Only ${limit - today:.4f} of today's allowance is left, under the ${self.cap:.2f} cap. "
                "Lower --max-usd or wait for the allowance to reset."
            )

    def estimate(self, kind: str) -> float:
        return max(FLOORS[kind], self.largest.get(kind, 0.0))

    def before(self, kind: str) -> None:
        if self.spent + self.estimate(kind) > self.cap:
            raise CapReachedError(
                f"${self.spent:.4f} spent; another {kind} case (up to ${self.estimate(kind):.4f}) could pass the "
                f"${self.cap:.2f} cap."
            )

    def after(self, kind: str, correlation_id: str) -> float:
        cost = ledger_cost(correlation_id)
        self.spent += cost
        self.largest[kind] = max(self.largest.get(kind, 0.0), cost)
        return cost
