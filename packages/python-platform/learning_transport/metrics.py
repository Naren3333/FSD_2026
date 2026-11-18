"""low-cardinality process measurements in prometheus text format; no user, resource or prompt labels."""

import threading
from typing import Any
from uuid import uuid4

_lock = threading.Lock()
_values: dict[str, float] = {}
_instance = str(uuid4())
_header_bounds_ms = (10, 50, 100, 500, 1000, 5000, 30_000, 60_000, 120_000, 300_000)
_header_counts = [0] * (len(_header_bounds_ms) + 1)


def observe_api_headers(seconds: float) -> None:
    bucket = next(
        (i for i, bound in enumerate(_header_bounds_ms) if seconds <= bound / 1000),
        len(_header_bounds_ms),
    )
    with _lock:
        _header_counts[bucket] += 1


def header_histogram() -> dict[str, str | list[int]]:
    with _lock:
        return {
            "instance": _instance,
            "bounds_ms": list(_header_bounds_ms),
            "bucket_counts": _header_counts.copy(),
        }


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
