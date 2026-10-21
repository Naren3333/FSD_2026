"""low-cardinality process measurements in prometheus text format; no user, resource or prompt labels."""

import threading
from typing import Any

_lock = threading.Lock()
_values: dict[str, float] = {}


def increment(name: str, value: float = 1.0) -> None:
    with _lock:
        _values[name] = _values.get(name, 0.0) + value


def observe(seconds: float, status: int) -> None:
    increment("learning_http_requests_total")
    increment("learning_http_headers_seconds_sum", seconds)
    if status >= 500:
        increment("learning_http_server_errors_total")
    for bound in (0.01, 0.05, 0.1, 0.5, 1, 5, 30, float("inf")):
        if seconds <= bound:
            label = "+Inf" if bound == float("inf") else str(bound)
            increment(f'learning_http_headers_seconds_bucket{{le="{label}"}}')


def render(extra: dict[str, float] | None = None) -> str:
    with _lock:
        values = {**_values, **(extra or {})}
    return "".join(f"{key} {value:g}\n" for key, value in sorted(values.items()))


def pool_metrics(state: Any) -> dict[str, float]:
    pool = getattr(state, "pool", None)
    if pool is None:
        for owner in ("ledger", "store"):
            pool = getattr(getattr(state, owner, None), "pool", None)
            if pool is not None:
                break
    return (
        {"learning_db_" + key: float(value) for key, value in pool.get_stats().items()}
        if pool is not None
        else {}
    )
