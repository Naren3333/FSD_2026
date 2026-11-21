"""selection and atomic checkpoints for paid, manually invoked evaluation runs."""

import json
from pathlib import Path


def select_cases(cases: list[dict], ids: list[str] | None) -> list[dict]:
    if not ids:
        return cases
    missing = set(ids) - {c["id"] for c in cases}
    if missing:
        raise ValueError("Unknown or excluded case IDs: " + ", ".join(sorted(missing)))
    return [c for c in cases if c["id"] in ids]


def ratio(part: float, whole: int) -> float | None:
    """part over whole to three places; none when there is nothing to divide by."""
    return round(part / whole, 3) if whole else None


def checkpoint(path: Path, report: dict) -> None:
    """replace the last complete json checkpoint; never truncate it in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)
