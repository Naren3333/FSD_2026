"""reading the reviewer's reply."""

import json
from typing import Any

import pytest

import checks
import prompts
import review
from review import Verdict

KEY_HOLDS = {"own_answer": "x", "evidence_supports_key": True, "other_option_also_correct": False}


def one(number: Any = 1, verdict: Any = "pass", reason: Any = "NONE", note: Any = "", **more: Any) -> dict:
    return {"question": number, **KEY_HOLDS, "verdict": verdict, "reason": reason, "note": note, **more}


def read(*reviews: dict, count: int) -> list[Verdict]:
    return review.verdicts(json.dumps({"reviews": list(reviews)}), count)


def test_one_verdict_per_draft_in_order():
    got = read(one(2, "fail", "WRONG_KEY", "The key is wrong."), one(1), count=2)
    assert got == [Verdict(True, "NONE", ""), Verdict(False, "WRONG_KEY", "The key is wrong.")]


def test_the_first_review_of_a_number_counts_and_other_numbers_are_ignored():
    got = read(one(1, "fail", "AMBIGUOUS", "Two fit."), one(1), one(0), one(3), one(True), one("1"), count=2)
    assert got == [Verdict(False, "AMBIGUOUS", "Two fit."), Verdict(False, "NOT_REVIEWED", "")]


def test_a_draft_with_no_review_is_not_reviewed():
    assert read(count=2) == [Verdict(False, "NOT_REVIEWED", "")] * 2


@pytest.mark.parametrize("reason", ["NONE", "MADE_UP", None, 3])
def test_a_fail_without_a_known_reason_is_not_reviewed(reason: Any):
    assert read(one(1, "fail", reason, "Something."), count=1) == [Verdict(False, "NOT_REVIEWED", "")]


def test_an_unknown_verdict_is_not_reviewed():
    assert read(one(1, "maybe", "WRONG_KEY"), count=1) == [Verdict(False, "NOT_REVIEWED", "")]


def test_a_pass_is_passed_whatever_the_reason_says():
    assert read(one(1, "pass", "WRONG_KEY", "ignored"), count=1) == [Verdict(True, "NONE", "")]


def test_notes_are_clipped_and_non_strings_are_empty():
    got = read(one(1, "fail", "WRONG_KEY", "n" * 400), one(2, "fail", "AMBIGUOUS", 7), count=2)
    assert got == [Verdict(False, "WRONG_KEY", "n" * 300), Verdict(False, "AMBIGUOUS", "")]


@pytest.mark.parametrize("output", ["nope", "[]", '{"reviews": "x"}', '{"other": []}'])
def test_an_unreadable_reply_raises(output: str):
    with pytest.raises(checks.UnreadableOutputError):
        review.verdicts(output, 1)


def test_non_object_reviews_are_skipped():
    assert review.verdicts(json.dumps({"reviews": ["x", None, one(1)]}), 1) == [Verdict(True, "NONE", "")]


def test_needs_materials_is_read_as_a_failure_with_that_reason():
    got = read(one(1, "fail", "NEEDS_MATERIALS", "Refers to the slides."), count=1)
    assert got == [Verdict(False, "NEEDS_MATERIALS", "Refers to the slides.")]


# ---- the reviewer's key checks (adr 0026)


def test_a_pass_whose_passages_do_not_support_the_key_fails_as_not_answerable():
    (got,) = read(one(1, evidence_supports_key=False), count=1)
    assert got == Verdict(False, "NOT_ANSWERABLE", review.KEY_UNSUPPORTED_NOTE, prompts.KEY_UNSUPPORTED)


def test_a_pass_with_a_second_right_option_fails_as_ambiguous():
    # the probe's transaction-guarantee draft: isolation is also guaranteed, so two options are right.
    (got,) = read(one(1, other_option_also_correct=True), count=1)
    assert got == Verdict(False, "AMBIGUOUS", review.SECOND_RIGHT_NOTE, prompts.SECOND_RIGHT_OPTION)


def test_the_option_check_applies_only_to_multiple_choice():
    output = json.dumps(
        {"reviews": [one(1, other_option_also_correct=True), one(2, other_option_also_correct=True)]}
    )
    assert review.verdicts(output, 2, [False, True]) == [
        Verdict(True, "NONE", ""),
        Verdict(False, "AMBIGUOUS", review.SECOND_RIGHT_NOTE, prompts.SECOND_RIGHT_OPTION),
    ]


@pytest.mark.parametrize("change", [{"evidence_supports_key": None}, {"other_option_also_correct": "no"}])
def test_a_key_check_left_out_or_not_a_boolean_fails_closed(change: dict[str, Any]):
    item = one(1)
    item.update(change)
    assert not read(item, count=1)[0].passed


def test_a_fail_keeps_the_reviewers_reason_with_the_key_checks_problem():
    (got,) = read(one(1, "fail", "WRONG_KEY", "Rollback undoes.", evidence_supports_key=False), count=1)
    assert got == Verdict(False, "WRONG_KEY", "Rollback undoes.", prompts.KEY_UNSUPPORTED)


def test_guidance_verdicts_need_only_the_evidence_check():
    passed = {
        "own_answer": "x",
        "evidence_supports_key": True,
        "verdict": "pass",
        "reason": "NONE",
        "note": "",
    }
    assert review.guidance_verdict(json.dumps(passed)) == Verdict(True, "NONE", "")
    unsupported = {**passed, "evidence_supports_key": False}
    assert review.guidance_verdict(json.dumps(unsupported)).reason == "NOT_ANSWERABLE"
    wrong = {**passed, "verdict": "fail", "reason": "WEAK_DISTRACTORS"}  # not a guidance reason
    assert review.guidance_verdict(json.dumps(wrong)).reason == "NOT_REVIEWED"
    with pytest.raises(checks.UnreadableOutputError):
        review.guidance_verdict("nope")
