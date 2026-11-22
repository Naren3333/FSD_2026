"""reading the reviewer's reply (adr 0006): one verdict per draft, never trusting the model to be complete; its key checks fail a draft it passed (adr 0026)."""

from dataclasses import dataclass, field
from typing import Any

import checks
from prompts import GUIDANCE_REVIEW_REASONS, KEY_UNSUPPORTED, REVIEW_REASONS, SECOND_RIGHT_OPTION

MAX_NOTE = 300
MAX_WEAK_OPTIONS = 8  # a multiple choice question's most options
NOT_REVIEWED = "NOT_REVIEWED"  # set here, not by the model
KEY_UNSUPPORTED_NOTE = "The cited passages do not support the answer key."
SECOND_RIGHT_NOTE = "Another option is also right according to the cited passages."


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str  # "NONE" when passed
    note: str  # clipped to 300 characters
    problem: str = ""  # what the generator is told, when more specific than the reason's words
    # the reviewer's own key checks held (adr 0026), and the wrong options it named on WEAK_DISTRACTORS; what
    # the pipeline acts on, not part of the verdict itself
    key_holds: bool = field(default=False, compare=False)
    weak_options: tuple[str, ...] = field(default=(), compare=False)


def _weak_options(review: dict[str, Any]) -> tuple[str, ...]:
    """the option texts the reviewer named as weak; anything that is not a list of strings is none."""
    named = review.get("weak_options")
    if not isinstance(named, list):
        return ()
    return tuple(o.strip() for o in named if isinstance(o, str) and o.strip())[:MAX_WEAK_OPTIONS]


def _key_checks(review: dict[str, Any], options: bool) -> tuple[str, str, str] | None:
    """the reason, note and problem when the reviewer's own key checks fail the draft; none when they hold. a missing or non-boolean answer is read as the failing one."""
    if options and review.get("other_option_also_correct") is not False:
        return "AMBIGUOUS", SECOND_RIGHT_NOTE, SECOND_RIGHT_OPTION
    if review.get("evidence_supports_key") is not True:
        return "NOT_ANSWERABLE", KEY_UNSUPPORTED_NOTE, KEY_UNSUPPORTED
    return None


def _verdict(review: dict[str, Any], reasons: list[str], options: bool = True) -> Verdict:
    note = review.get("note")
    note = note.strip()[:MAX_NOTE] if isinstance(note, str) else ""
    failed = _key_checks(review, options)
    if review.get("verdict") == "pass":
        if failed is None:
            return Verdict(True, "NONE", "", key_holds=True)
        return Verdict(False, *failed)
    reason = review.get("reason")
    if review.get("verdict") == "fail" and reason in reasons and reason != "NONE":
        weak = _weak_options(review) if reason == "WEAK_DISTRACTORS" else ()
        return Verdict(False, reason, note, failed[2] if failed else "", failed is None, weak)
    return Verdict(False, NOT_REVIEWED, "")


def verdicts(output: str, count: int, choices: list[bool] | None = None) -> list[Verdict]:
    """one verdict per draft 1..count, in order, `choices` saying which are multiple choice (all, when not given); raises checks.UnreadableOutputError if the reply is not a json object with a reviews list."""
    reviews = checks.read_reply(output).get("reviews")
    if not isinstance(reviews, list):
        raise checks.UnreadableOutputError
    found: dict[int, Verdict] = {}
    for review in reviews:
        number = review.get("question") if isinstance(review, dict) else None
        if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= count:
            options = choices is None or choices[number - 1]
            found.setdefault(number, _verdict(review, REVIEW_REASONS, options))
    return [found.get(n, Verdict(False, NOT_REVIEWED, "")) for n in range(1, count + 1)]


def guidance_verdict(output: str) -> Verdict:
    """the guidance reviewer's verdict; raises checks.UnreadableOutputError if the reply is not a json object."""
    return _verdict(checks.read_reply(output), GUIDANCE_REVIEW_REASONS, options=False)
