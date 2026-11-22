"""the chat eval scores the support check: verdict and uncited rates, and the unsupported_notice expectation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import run_chat


def digest(
    outcome: str = "answered", check: dict | None = None, cited: int = 1, version: str = "student_tutor.v4"
) -> dict:
    return {"outcome": outcome, "prompt_version": version, "cited": ["x"] * cited, "answer_check": check}


def checked(*verdicts: str) -> dict:
    rows = [{"statement": f"claim {n}", "verdict": v} for n, v in enumerate(verdicts)]
    counts = {v: list(verdicts).count(v) for v in run_chat.VERDICTS}
    return {"status": "checked", "counts": counts, "statements": rows}


def test_rates_count_checked_statements_and_uncited_answers() -> None:
    turns = [
        digest(check=checked("supported", "supported", "partial", "unsupported")),
        digest(check={"status": "uncited"}, cited=0),
        digest(check={"status": "failed"}),
        digest(outcome="no_results"),  # refusals are not answers
        digest(version="small_talk.v1", cited=0),  # nor is small talk
    ]
    assert run_chat.faithfulness(turns) == {
        "answered": 3, "checked": 1, "check_failed": 1, "statements": 4,
        "supported_rate": 0.5, "partial_rate": 0.25, "unsupported_rate": 0.25, "uncited_rate": 0.333,
    }  # fmt: skip
    assert run_chat.faithfulness([])["uncited_rate"] is None


@pytest.mark.parametrize(
    ("expected", "turn", "failed", "flagged"),
    [
        (None, digest(check=checked("unsupported")), False, True),
        (None, digest(check=checked("supported")), False, False),
        (True, digest(check=checked("supported", "partial")), True, False),
        (True, digest(check=checked("unsupported")), False, False),
        (True, digest(outcome="no_results"), False, False),  # a refusal is fine
        (False, digest(check=checked("unsupported")), True, False),
        (None, digest(check={"status": "failed"}), False, True),
    ],
)
def test_the_unsupported_notice_expectation(
    expected: bool | None, turn: dict, failed: bool, flagged: bool
) -> None:
    case = {} if expected is None else {"unsupported_notice": expected}
    failures: list[str] = []
    flags: list[str] = []
    run_chat.judge_support(case, "turn 1", turn, failures, flags)
    assert (bool(failures), bool(flags)) == (failed, flagged)


def test_the_summary_reports_time_to_done_apart_from_the_saved_latency() -> None:
    turn = {
        "outcome": "answered",
        "prompt_version": "student_tutor.v4",
        "usage_tokens": 1,
        "answer_check": None,
    }
    cases = [
        {"group": "support", "failures": [], "flags": [],
         "results": [{"digest": {**turn, "latency_ms": 900, "done_ms": 1200}},
                     {"digest": {**turn, "latency_ms": 700, "done_ms": None}}]},
    ]  # fmt: skip
    summary = run_chat.summarize(cases)
    assert (summary["done_ms_p50"], summary["done_ms_max"]) == (1200, 1200)
    assert summary["latency_ms_max"] == 900


def test_every_case_group_is_one_the_runner_reports() -> None:
    cases = run_chat.yaml.safe_load(run_chat.CASES.read_text(encoding="utf-8"))["cases"]
    assert {c["group"] for c in cases} <= set(run_chat.GROUPS)
    support = [c for c in cases if c["group"] == "support"]
    # each says whether a notice is expected, so a false alarm or a miss fails the run.
    assert len(support) >= 2 and all(isinstance(c.get("unsupported_notice"), bool) for c in support)
