"""reading the reviewer's reply (adr 0006): one verdict per draft, never trusting the model to be complete."""

from dataclasses import dataclass
from typing import Any

import checks
from prompts import REVIEW_REASONS

MAX_NOTE = 300
NOT_REVIEWED = "NOT_REVIEWED"  # set here, not by the model


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str  # "NONE" when passed
    note: str  # clipped to 300 characters


def _verdict(review: dict[str, Any]) -> Verdict:
    note = review.get("note")
    note = note.strip()[:MAX_NOTE] if isinstance(note, str) else ""
    if review.get("verdict") == "pass":
        return Verdict(True, "NONE", "")
    reason = review.get("reason")
    if review.get("verdict") == "fail" and reason in REVIEW_REASONS and reason != "NONE":
        return Verdict(False, reason, note)
    return Verdict(False, NOT_REVIEWED, "")


def verdicts(output: str, count: int) -> list[Verdict]:
    """one verdict per draft 1..count, in order; raises checks.UnreadableOutputError if the reply is not a json object with a reviews list."""
    reviews = checks.read_reply(output).get("reviews")
    if not isinstance(reviews, list):
        raise checks.UnreadableOutputError
    found: dict[int, Verdict] = {}
    for review in reviews:
        number = review.get("question") if isinstance(review, dict) else None
        if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= count:
            found.setdefault(number, _verdict(review))
    return [found.get(n, Verdict(False, NOT_REVIEWED, "")) for n in range(1, count + 1)]
