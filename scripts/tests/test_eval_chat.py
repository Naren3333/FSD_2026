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


def test_uncited_topics_are_the_chatbots_own_count_over_its_sub_queries() -> None:
    chunks = {"c1": {"title": "Software Design"}, "c2": {"title": "Software Testing"}}
    case = multi_case(
        {
            "c1": {"primary": "design", "topics": ["design", "testing"]},
            "c2": {"primary": "testing", "topics": ["testing"]},
        }
    )
    run_chat.assess(case, chunks, [])
    # c1 was found for testing too, but testing has a passage of its own.
    assert case["results"][0]["digest"]["uncited_topics"] == ["testing"]
    older = multi_case(None)
    run_chat.assess(older, chunks, [])
    assert older["results"][0]["digest"]["uncited_topics"] is None


def multi_case(passage_topics: dict | None) -> dict:
    saved = {"id": "m", "outcome": "answered", "prompt_version": "student_tutor.v5", "rewritten_query": "q",
             "sub_queries": ["design", "testing"], "content": "Design [doc:c1].", "retrieved": ["c1", "c2"],
             "citations": [{"chunk_id": "c1", "title": "Software Design", "page_start": 1}],
             "usage_tokens": 1, "latency_ms": 1, "answer_check": None, "passage_topics": passage_topics}  # fmt: skip
    return {"id": "multi", "group": "multitopic", "as": "t1", "topics": [["Software Design"], ["Software Testing"]],
            "results": [{"saved": saved, "sse_error": None}]}  # fmt: skip


def test_a_failed_multitopic_case_says_which_topic_had_no_cited_primary_passage() -> None:
    chunks = {"c1": {"title": "Software Design"}, "c2": {"title": "Software Testing"}}
    case = multi_case(
        {
            "c1": {"primary": "design", "topics": ["design"]},
            "c2": {"primary": "testing", "topics": ["testing"]},
        }
    )
    run_chat.assess(case, chunks, [])
    assert case["failures"] == ["turn 1: nothing cited from Software Testing (retrieved)"]
    assert "turn 1: topics with no cited primary passage: testing" in case["flags"]
    older = multi_case(None)  # an answer saved before passage topics were
    run_chat.assess(older, chunks, [])
    assert any("unknown (no passage topics saved)" in f for f in older["flags"])


def test_the_probe_scores_catches_notices_and_false_alarms() -> None:
    batch = {"id": "b", "statements": [
        {"id": "f1", "kind": "number", "expect": "partial"},
        {"id": "f2", "kind": "contradiction", "expect": "unsupported"},
        {"id": "f3", "kind": "outside", "expect": "unsupported"},
        {"id": "f4", "kind": "name", "expect": "partial"},
        {"id": "k1", "kind": "paraphrase", "expect": "supported"},
        {"id": "k2", "kind": "hint", "expect": "supported"},
    ]}  # fmt: skip
    got = {"status": "checked", "verdicts": {"f1": "partial", "f2": "unsupported", "f3": "supported",
                                             "f4": "unsupported", "k1": "partial", "k2": "unsupported"}}  # fmt: skip
    rows = run_chat.probe_rows(batch, got, 1) + run_chat.probe_rows(batch, {"status": "failed"}, 2)
    summary = run_chat.probe_summary(rows)
    assert {k: summary[k] for k in ("statements", "check_failed", "catch_rate", "notice_rate",
                                    "control_partial_rate", "false_alarm_rate")} == {
        "statements": 12, "check_failed": 6, "catch_rate": 0.75, "notice_rate": 0.5,
        "control_partial_rate": 0.5, "false_alarm_rate": 0.5}  # fmt: skip
    assert (
        summary["missed"] == ["f3"] and summary["no_notice"] == ["f1"] and summary["false_alarms"] == ["k2"]
    )
    assert summary["by_kind"]["contradiction"] == "0 supported 0 partial 1 unsupported of 1"
    assert run_chat.probe_summary([])["catch_rate"] is None


def test_the_probe_set_has_faults_of_each_kind_and_controls() -> None:
    batches = run_chat.probe_batches()
    statements = [s for b in batches for s in b["statements"]]
    assert len(statements) >= 20 and len({s["id"] for s in statements}) == len(statements)
    kinds = {s["kind"] for s in statements}
    assert kinds == {"number", "name", "contradiction", "outside", "paraphrase", "condensing", "hint"}
    for s in statements:
        assert s["expect"] in run_chat.VERDICTS
        assert (s["expect"] == "supported") == (s["kind"] in ("paraphrase", "condensing", "hint"))
        assert s["cites"] and all(
            c in next(b for b in batches if s in b["statements"])["passages"] for c in s["cites"]
        )
