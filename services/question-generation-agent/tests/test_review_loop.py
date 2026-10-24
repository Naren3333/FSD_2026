"""generate, review and retry end to end against fakes (adr 0006): what each model call is sent, who pays, and what the teacher gets back."""

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from conftest import CHUNK_1, CHUNK_2, DOC, SETTINGS, Fakes, as_user, reply
from test_api import BODY, GENERATE
from test_checks import choice, short

import main
import pipeline
import prompts
from contracts import DraftQuestion, Generated, GenerateRequest, Review
from review import Verdict

STEMS = [
    "What does a unit test check?",
    "Why are dependencies replaced with test doubles?",
    "Which values sit at the edges of an input range?",
    "Name one place where off-by-one mistakes happen.",
    "How does a failing unit test help a developer?",
    "When is a payment service replaced in a test?",
]
GEN, REV = "questions.generated", "questions.reviewed"
WRONG_KEY = ("WRONG_KEY", "The key does not match the passage.")
WEAK = ("WEAK_DISTRACTORS", "The wrong options are absurd.")


@pytest.fixture(autouse=True)
def reviewing(api) -> None:
    configure()


def configure(**change: Any) -> None:
    main.app.state.settings = replace(SETTINGS, review=True, **change)


def mc(n: int, **change: Any) -> dict[str, Any]:
    return choice(prompt=STEMS[n], **change)


def gen(*items: dict[str, Any]) -> httpx.Response:
    return reply({"questions": list(items)})


def rev(*verdicts: str | tuple[str, str]) -> httpx.Response:
    """a review reply: "pass", or a (reason, note) that fails the draft."""
    reviews = [
        {"question": i, "own_answer": "x", "verdict": "pass", "reason": "NONE", "note": ""}
        if v == "pass"
        else {"question": i, "own_answer": "x", "verdict": "fail", "reason": v[0], "note": v[1]}
        for i, v in enumerate(verdicts, start=1)
    ]
    return reply({"reviews": reviews})


def ask(api, **change: Any) -> httpx.Response:
    """count=n is shorthand for n multiple-choice questions; three of them when nothing is said."""
    counts = {"multiple_choice": change.pop("count", 3)}
    return api.post(GENERATE, json={**BODY, "counts": counts, **change}, headers=as_user())


def sent(fakes: Fakes, *schemas: str) -> list[dict[str, Any]]:
    """the ai-core calls: how many, in what order, each billed to the teacher."""
    calls = fakes.calls("/v1/inference")
    assert [c["schema_name"] for c in calls] == list(schemas)
    assert all(c["billed_to"] == {"user_id": "t1", "org_id": "org", "role": "teacher"} for c in calls)
    return calls


def task(call: dict[str, Any]) -> dict[str, Any]:
    return json.loads(call["messages"][1]["content"])


def statuses(data: dict[str, Any]) -> list[tuple[str, int]]:
    return [(q["review"]["status"], q["review"]["round"]) for q in data["questions"]]


def test_everything_passes_in_one_round(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1), mc(2)), rev("pass", "pass", "pass")]
    resp = ask(api)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    sent(fakes, GEN, REV)
    assert statuses(data) == [("passed", 0)] * 3 and data["model_calls"] == 2 and data["dropped"] == {}
    assert data["rounds"] == [
        {
            "generated": 3,
            "passed_checks": 3,
            "passed_review": 3,
            "rejected": {},
            "reviewed": True,
            "retried": False,
            "sent_back": {},
        }
    ]


def test_a_rejected_key_is_sent_back_and_the_shortfall_rewritten(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1), mc(2)), rev("pass", "pass", WRONG_KEY), gen(mc(3)), rev("pass")]
    data = ask(api).json()
    calls = sent(fakes, GEN, REV, GEN, REV)
    retry = task(calls[2])
    assert retry["counts"] == {"multiple_choice": 1} and "count" not in retry
    assert retry["previous_attempts"] == [
        {"prompt": STEMS[2], "type": "multiple_choice", "problem": prompts.PROBLEMS["WRONG_KEY"]}
    ]
    assert STEMS[0] in retry["existing_questions"] and STEMS[1] in retry["existing_questions"]
    assert [q["question"]["prompt"] for q in data["questions"]] == [STEMS[0], STEMS[1], STEMS[3]]
    assert statuses(data) == [("passed", 0), ("passed", 0), ("passed", 1)]
    assert data["dropped"] == {"WRONG_KEY": 1} and data["model_calls"] == 4
    assert [r["rejected"] for r in data["rounds"]] == [{"WRONG_KEY": 1}, {}]
    # the reviewer of round 1 sees the two accepted questions as existing, and only the new draft.
    review = task(calls[3])
    assert [q["prompt"] for q in review["questions"]] == [STEMS[3]]
    assert review["existing_questions"] == [STEMS[0], STEMS[1]]


def test_a_failed_check_is_sent_back_the_same_way(api, fakes: Fakes):
    bad = mc(2, evidence="This sentence is nowhere in the passages.")
    fakes.replies = [gen(mc(0), mc(1), bad), rev("pass", "pass"), gen(mc(3)), rev("pass")]
    data = ask(api).json()
    calls = sent(fakes, GEN, REV, GEN, REV)
    (attempt,) = task(calls[2])["previous_attempts"]
    assert attempt["prompt"] == STEMS[2] and attempt["problem"] == prompts.PROBLEMS["EVIDENCE_NOT_FOUND"]
    assert data["returned"] == 3 and data["dropped"] == {"EVIDENCE_NOT_FOUND": 1}


def test_still_failing_every_round_stops_after_three_rounds(api, fakes: Fakes):
    fakes.replies = [
        gen(mc(0), mc(1)),
        rev("pass", WRONG_KEY),
        gen(mc(2)),
        rev(WRONG_KEY),
        gen(mc(3)),
        rev(WRONG_KEY),
    ]
    data = ask(api, count=2).json()
    sent(fakes, GEN, REV, GEN, REV, GEN, REV)
    assert data["returned"] == 1 and data["dropped"] == {"WRONG_KEY": 3} and data["model_calls"] == 6
    assert len(data["rounds"]) == 3


def test_no_retry_starts_after_the_deadline(api, fakes: Fakes):
    configure(review_deadline=0)
    fakes.replies = [gen(mc(0), mc(1)), rev("pass", WRONG_KEY)]
    data = ask(api, count=2).json()
    sent(fakes, GEN, REV)
    assert data["returned"] == 1 and data["model_calls"] == 2 and len(data["rounds"]) == 1


def test_an_unreadable_review_leaves_the_drafts_not_reviewed(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1)), httpx.Response(200, json={"output": "nope"})]
    resp = ask(api, count=2)
    assert resp.status_code == 200
    data = resp.json()
    sent(fakes, GEN, REV)
    assert statuses(data) == [("not_reviewed", 0)] * 2 and data["model_calls"] == 2
    assert all(q["review"]["note"] for q in data["questions"])


def test_a_review_stopped_by_the_allowance_still_returns_the_drafts(api, fakes: Fakes):
    limit = {"code": "AI_SPEND_LIMIT", "message": "x", "resets_at": "2026-10-18T00:00:00+08:00"}
    fakes.replies = [gen(mc(0), mc(1)), httpx.Response(429, json={"error": limit})]
    resp = ask(api, count=2)
    assert resp.status_code == 200
    data = resp.json()
    sent(fakes, GEN, REV)
    assert statuses(data) == [("not_reviewed", 0)] * 2 and data["model_calls"] == 1


def test_the_first_generate_call_stopped_by_the_allowance_is_still_a_429(api, fakes: Fakes):
    limit = {"code": "AI_SPEND_LIMIT", "message": "x"}
    fakes.replies = [httpx.Response(429, json={"error": limit})]
    resp = ask(api)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "AI_SPEND_LIMIT"
    sent(fakes, GEN)


def test_a_failed_retry_keeps_what_was_accepted(api, fakes: Fakes):
    failed = {"code": "AI_PROVIDER_FAILED", "message": "x"}
    fakes.replies = [gen(mc(0), mc(1)), rev("pass", WRONG_KEY), httpx.Response(502, json={"error": failed})]
    resp = ask(api, count=2)
    assert resp.status_code == 200
    data = resp.json()
    sent(fakes, GEN, REV, GEN)
    assert data["returned"] == 1 and data["dropped"] == {"WRONG_KEY": 1} and data["model_calls"] == 2


def test_a_retry_stopped_by_the_allowance_with_nothing_accepted_is_a_429(api, fakes: Fakes):
    limit = {"code": "AI_SPEND_LIMIT", "message": "x"}
    fakes.replies = [gen(mc(0)), rev(WRONG_KEY), httpx.Response(429, json={"error": limit})]
    resp = ask(api, count=1)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "AI_SPEND_LIMIT"


def test_every_round_failing_the_checks_is_rejected(api, fakes: Fakes):
    bad = {"evidence": "This sentence is nowhere in the passages."}
    fakes.replies = [gen(mc(0, **bad)), gen(mc(1, **bad)), gen(mc(2, **bad))]
    resp = ask(api, count=1)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "GENERATION_REJECTED"
    sent(fakes, GEN, GEN, GEN)


def test_a_teacher_can_skip_the_review(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1), mc(2))]
    data = ask(api, review=False).json()
    sent(fakes, GEN)
    assert statuses(data) == [("not_reviewed", 0)] * 3 and data["model_calls"] == 1
    assert data["rounds"] == [
        {
            "generated": 3,
            "passed_checks": 3,
            "passed_review": 3,
            "rejected": {},
            "reviewed": False,
            "retried": False,
            "sent_back": {},
        }
    ]


def test_the_server_can_switch_the_review_off(api, fakes: Fakes):
    main.app.state.settings = replace(SETTINGS, review=False)
    fakes.replies = [gen(mc(0), mc(1), mc(2))]
    data = ask(api).json()
    sent(fakes, GEN)
    assert statuses(data) == [("not_reviewed", 0)] * 3 and data["model_calls"] == 1


def test_weak_distractors_in_the_last_round_are_flagged_after_the_passed_drafts(api, fakes: Fakes):
    configure(review_retries=0)
    fakes.replies = [gen(mc(0), mc(1)), rev(WEAK, "pass")]
    data = ask(api, count=2).json()
    sent(fakes, GEN, REV)
    assert [q["question"]["prompt"] for q in data["questions"]] == [STEMS[1], STEMS[0]]
    assert data["questions"][1]["review"] == {
        "status": "flagged",
        "note": WEAK[1],
        "round": 0,
        "source": "reviewer",
    }
    assert data["questions"][0]["review"]["status"] == "passed" and data["dropped"] == {}


def test_a_flagged_draft_is_dropped_when_a_retry_fills_the_count(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1)), rev("pass", WEAK), gen(mc(2)), rev("pass")]
    data = ask(api, count=2).json()
    sent(fakes, GEN, REV, GEN, REV)
    assert [q["question"]["prompt"] for q in data["questions"]] == [STEMS[0], STEMS[2]]
    assert statuses(data) == [("passed", 0), ("passed", 1)] and data["dropped"] == {"WEAK_DISTRACTORS": 1}
    assert data["rounds"][0]["rejected"] == {"WEAK_DISTRACTORS": 1}


def test_the_reviewer_is_sent_neither_tokens_nor_ids_nor_the_explanation(api, fakes: Fakes):
    fakes.replies = [gen(mc(0, explanation="Zebra-striped reasoning for the teacher.")), rev("pass")]
    ask(api, count=1)
    review = sent(fakes, GEN, REV)[1]
    # the evidence is a quote from the passage, so only its key can be absent.
    text = json.dumps(review["messages"])
    assert all(x not in text for x in (CHUNK_1, CHUNK_2, DOC, "Zebra", "evidence", "explanation", "Bearer"))
    assert "Bearer" not in json.dumps(review) and CHUNK_1 not in json.dumps(review)


def test_the_retry_asks_only_for_the_types_still_short(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1), short()), rev("pass", WRONG_KEY, "pass"), gen(mc(3)), rev("pass")]
    data = ask(api, counts={"multiple_choice": 2, "short_answer": 1}).json()
    retry = task(sent(fakes, GEN, REV, GEN, REV)[2])
    assert retry["counts"] == {"multiple_choice": 1}
    assert [q["question"]["type"] for q in data["questions"]] == [
        "multiple_choice",
        "multiple_choice",
        "short_answer",
    ]
    assert data["requested"] == 3 and data["requested_by_type"] == {"multiple_choice": 2, "short_answer": 1}


def test_the_questions_come_back_grouped_by_type_and_by_review_status(api, fakes: Fakes):
    configure(review_retries=0)
    fakes.replies = [gen(short(), mc(0), mc(1)), rev("pass", WEAK, "pass")]
    data = ask(api, counts={"multiple_choice": 2, "short_answer": 1}).json()
    assert [(q["question"]["type"], q["review"]["status"]) for q in data["questions"]] == [
        ("multiple_choice", "passed"),
        ("multiple_choice", "flagged"),
        ("short_answer", "passed"),
    ]
    assert data["questions"][0]["question"]["prompt"] == STEMS[1]


def test_without_a_review_each_type_is_cut_to_its_own_count(api, fakes: Fakes):
    fakes.replies = [gen(short(), mc(0), mc(1))]
    data = ask(api, review=False, counts={"multiple_choice": 1, "short_answer": 1}).json()
    assert [q["question"]["type"] for q in data["questions"]] == ["multiple_choice", "short_answer"]
    assert data["returned"] == 2 and data["dropped"] == {}
    assert data["requested_by_type"] == {"multiple_choice": 1, "short_answer": 1}


def test_a_draft_that_needs_the_materials_is_sent_back_and_dropped_after_the_last_round(api, fakes: Fakes):
    configure(review_retries=1)
    needs = ("NEEDS_MATERIALS", "Refers to the slides.")
    fakes.replies = [gen(mc(0)), rev(needs), gen(mc(1)), rev(needs)]
    resp = ask(api, count=1)
    calls = sent(fakes, GEN, REV, GEN, REV)
    problem = task(calls[2])["previous_attempts"][0]["problem"]
    assert "stands on its own" in problem
    assert resp.status_code == 422


LONG = "A test that checks one function or class on its own with its dependencies replaced"


def long_key(n: int) -> dict[str, Any]:
    return mc(n, options=[LONG, "An end to end test", "A load test"], answer=LONG)


def test_a_long_key_is_reviewed_and_a_pass_is_sent_back_then_flagged(api, fakes: Fakes):
    configure(review_retries=0)
    fakes.replies = [gen(long_key(0)), rev("pass")]
    data = ask(api, count=1).json()
    review_call = sent(fakes, GEN, REV)[1]
    assert [q["prompt"] for q in task(review_call)["questions"]] == [STEMS[0]]
    assert data["questions"][0]["review"] == {
        "status": "flagged",
        "note": "The answer is much longer than the other options.",
        "round": 0,
        "source": "check",
    }
    assert data["dropped"] == {}
    assert data["rounds"] == [
        {
            "generated": 1,
            "passed_checks": 1,
            "passed_review": 0,
            "rejected": {"LONG_KEY": 1},
            "reviewed": True,
            "retried": False,
            "sent_back": {"LONG_KEY": 1},
        }
    ]


def test_a_long_key_that_passes_is_replaced_by_a_retry(api, fakes: Fakes):
    fakes.replies = [gen(long_key(0)), rev("pass"), gen(mc(1)), rev("pass")]
    data = ask(api, count=1).json()
    calls = sent(fakes, GEN, REV, GEN, REV)
    assert task(calls[2])["previous_attempts"][0]["prompt"] == STEMS[0]
    assert task(calls[2])["previous_attempts"][0]["problem"] == prompts.PROBLEMS["LONG_KEY"]
    assert [q["question"]["prompt"] for q in data["questions"]] == [STEMS[1]]
    assert data["dropped"] == {"LONG_KEY": 1}


def test_a_long_key_the_reviewer_fails_is_dropped_with_the_reviewers_reason(api, fakes: Fakes):
    configure(review_retries=0)
    fakes.replies = [gen(long_key(0), mc(1)), rev(WRONG_KEY, "pass")]
    data = ask(api, count=2).json()
    sent(fakes, GEN, REV)
    assert [q["question"]["prompt"] for q in data["questions"]] == [STEMS[1]]
    assert data["dropped"] == {"WRONG_KEY": 1}
    assert data["rounds"][0]["rejected"] == {"WRONG_KEY": 1} and data["rounds"][0]["passed_review"] == 1


def test_without_a_review_a_long_key_is_flagged_not_dropped(api, fakes: Fakes):
    long = "A test that checks one function or class on its own with its dependencies replaced"
    item = mc(0, options=[long, "An end to end test", "A load test"], answer=long)
    fakes.replies = [gen(item)]
    data = ask(api, count=1, review=False).json()
    review = data["questions"][0]["review"]
    assert (review["status"], review["source"]) == ("flagged", "check") and data["dropped"] == {}


def test_a_corrected_rewrite_of_a_rejected_draft_is_kept_not_a_duplicate(api, fakes: Fakes):
    rewrite = choice(
        prompt=STEMS[0].replace("What", "Exactly what"),
        options=["The whole system end to end", "A single function or class in isolation"],
        answer="A single function or class in isolation",
    )
    wrong = mc(0, answer="The whole system end to end")
    fakes.replies = [gen(wrong), rev(WRONG_KEY), gen(rewrite), rev("pass")]
    resp = ask(api, count=1)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    retry = task(sent(fakes, GEN, REV, GEN, REV)[2])
    assert STEMS[0] not in retry["existing_questions"]
    assert [a["prompt"] for a in retry["previous_attempts"]] == [STEMS[0]]
    assert [q["question"]["prompt"] for q in data["questions"]] == [rewrite["prompt"]]
    assert data["dropped"] == {"WRONG_KEY": 1}


class Clock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now


def tick(monkeypatch: pytest.MonkeyPatch, seconds_per_call: float) -> None:
    """a fake clock that moves on by `seconds_per_call` for every model call."""
    clock = Clock()
    ask_model = pipeline.Pipeline._ask

    async def slow(self: Any, *args: Any) -> str:
        clock.now += seconds_per_call
        return await ask_model(self, *args)

    monkeypatch.setattr(pipeline, "time", clock)
    monkeypatch.setattr(pipeline.Pipeline, "_ask", slow)


def test_no_retry_starts_when_it_would_not_finish_inside_the_deadline(
    api, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
):
    configure(review_deadline=240)
    tick(monkeypatch, 50)  # round 0 takes 100 s, and 100 + 1.5 * 100 is over 240 s
    fakes.replies = [gen(mc(0), mc(1)), rev("pass", WRONG_KEY)]
    data = ask(api, count=2).json()
    sent(fakes, GEN, REV)
    assert data["returned"] == 1 and len(data["rounds"]) == 1


def test_a_retry_runs_when_it_fits_inside_the_deadline(api, fakes: Fakes, monkeypatch: pytest.MonkeyPatch):
    configure(review_deadline=240)
    tick(monkeypatch, 30)  # round 0 takes 60 s, and 60 + 1.5 * 60 fits
    fakes.replies = [gen(mc(0), mc(1)), rev("pass", WRONG_KEY), gen(mc(2)), rev("pass")]
    data = ask(api, count=2).json()
    sent(fakes, GEN, REV, GEN, REV)
    assert data["returned"] == 2 and len(data["rounds"]) == 2


def test_a_short_reply_with_nothing_rejected_is_written_again(api, fakes: Fakes):
    fakes.replies = [
        gen(mc(0), mc(1)),
        rev("pass", "pass"),
        gen(mc(2), mc(3), mc(4)),
        rev("pass", "pass", "pass"),
    ]
    data = ask(api, count=5).json()
    calls = sent(fakes, GEN, REV, GEN, REV)
    assert task(calls[2])["counts"] == {"multiple_choice": 3}
    assert data["returned"] == 5 and len(data["rounds"]) == 2


def test_an_empty_reply_is_written_again_not_rejected(api, fakes: Fakes):
    fakes.replies = [gen(), gen(mc(0)), rev("pass")]
    resp = ask(api, count=1)
    assert resp.status_code == 200, resp.text
    sent(fakes, GEN, GEN, REV)


def test_a_draft_the_reviewer_skips_comes_back_not_reviewed_without_a_retry(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1), mc(2)), rev("pass", "pass")]
    data = ask(api).json()
    sent(fakes, GEN, REV)
    assert statuses(data) == [("passed", 0), ("passed", 0), ("not_reviewed", 0)]
    assert data["questions"][2]["review"]["note"] == "The reviewer gave no verdict on this draft."
    assert data["dropped"] == {} and data["model_calls"] == 2
    assert data["rounds"][0]["rejected"] == {"NOT_REVIEWED": 1} and data["rounds"][0]["passed_review"] == 2


def test_drafts_the_reviewer_skips_entirely_are_returned_not_rejected(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1)), rev()]
    resp = ask(api, count=2)
    assert resp.status_code == 200, resp.text
    sent(fakes, GEN, REV)
    assert statuses(resp.json()) == [("not_reviewed", 0)] * 2


def test_a_long_key_left_out_by_the_cut_is_counted_under_its_own_reason():
    run = pipeline.Run()
    kept = [GeneratedOf(mc(0)), GeneratedOf(mc(1))]
    run.judged(0, kept, [Verdict(True, "NONE", ""), Verdict(True, "NONE", "")], {}, {})
    run.weak.append((GeneratedOf(long_key(2)), "LONG_KEY"))
    final = run.assembled(GenerateRequest.model_validate({**BODY, "counts": {"multiple_choice": 2}}))
    assert len(final) == 2 and run.dropped == {"LONG_KEY": 1}


def GeneratedOf(item: dict[str, Any]) -> Generated:  # noqa: N802
    """a draft without validation, for the parts of Run that only read its type, prompt and options."""
    draft = DraftQuestion.model_construct(
        **{k: v for k, v in item.items() if k not in ("evidence", "explanation")}
    )
    return Generated.model_construct(
        question=draft, evidence=item["evidence"], explanation=item["explanation"]
    )


def test_a_retried_round_is_marked_and_the_last_round_is_not(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1)), rev("pass", WRONG_KEY), gen(mc(2)), rev("pass")]
    rounds = ask(api, count=2).json()["rounds"]
    assert [(r["reviewed"], r["retried"]) for r in rounds] == [(True, True), (True, False)]


def test_a_round_whose_review_did_not_run_is_not_marked_reviewed(api, fakes: Fakes):
    fakes.replies = [gen(mc(0)), httpx.Response(200, json={"output": "nope"})]
    (round_,) = ask(api, count=1).json()["rounds"]
    assert (round_["reviewed"], round_["retried"]) == (False, False)


def test_a_corrected_rewrite_of_a_weak_draft_replaces_it_not_a_duplicate(api, fakes: Fakes):
    better = mc(0, options=["A single function in isolation", "The whole system", "A load test"])
    better["answer"] = "A single function in isolation"
    fakes.replies = [gen(mc(0)), rev(WEAK), gen(better), rev("pass")]
    resp = ask(api, count=1)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    retry = task(sent(fakes, GEN, REV, GEN, REV)[2])
    assert STEMS[0] not in retry["existing_questions"]
    assert [a["prompt"] for a in retry["previous_attempts"]] == [STEMS[0]]
    assert [q["question"]["options"] for q in data["questions"]] == [better["options"]]
    assert statuses(data) == [("passed", 1)] and "DUPLICATE" not in data["dropped"]


REWRITE = "Exactly what does a unit test check?"


def rewrite_of(n: int) -> dict[str, Any]:
    return choice(prompt=STEMS[n].replace("What", "Exactly what").replace("Why", "Exactly why"))


def test_a_weak_draft_and_its_passed_rewrite_are_not_both_returned(api, fakes: Fakes):
    a2, b2 = rewrite_of(0), rewrite_of(1)
    fakes.replies = [gen(mc(0), mc(1)), rev(WEAK, WEAK), gen(a2, b2), rev("pass", WEAK)]
    configure(review_retries=1)
    data = ask(api, count=2).json()
    assert [q["question"]["prompt"] for q in data["questions"]] == [a2["prompt"], b2["prompt"]]
    assert data["dropped"] == {"WEAK_DISTRACTORS": 2}


def test_the_newest_of_two_weak_drafts_is_returned(api, fakes: Fakes):
    a2 = rewrite_of(0)
    fakes.replies = [gen(mc(0)), rev(WEAK), gen(a2), rev(WEAK), gen(mc(5)), rev(WEAK)]
    data = ask(api, count=1).json()
    assert [q["question"]["prompt"] for q in data["questions"]] != [STEMS[0]]
    assert [q["review"]["round"] for q in data["questions"]] == [2]


def test_two_weak_drafts_that_are_not_alike_are_both_kept(api, fakes: Fakes):
    configure(review_retries=0)
    fakes.replies = [gen(mc(0), mc(1)), rev(WEAK, WEAK)]
    data = ask(api, count=2).json()
    assert [q["question"]["prompt"] for q in data["questions"]] == [STEMS[0], STEMS[1]]
    assert data["dropped"] == {}


def test_a_weak_draft_replaced_by_a_rewrite_is_counted_as_dropped():
    run = pipeline.Run()
    old, new = GeneratedOf(mc(0)), GeneratedOf(rewrite_of(0))
    run.weak += [
        (old.model_copy(update={"review": Review(status="flagged", round=0)}), "WEAK_DISTRACTORS"),
        (new.model_copy(update={"review": Review(status="flagged", round=1)}), "WEAK_DISTRACTORS"),
    ]
    final = run.assembled(GenerateRequest.model_validate({**BODY, "counts": {"multiple_choice": 2}}))
    assert [g.question.prompt for g in final] == [rewrite_of(0)["prompt"]]
    assert run.dropped == {"WEAK_DISTRACTORS": 1}


def test_a_retry_outage_after_failed_checks_is_reported_as_an_outage(api, fakes: Fakes):
    bad = {"evidence": "This sentence is nowhere in the passages."}
    down = {"code": "AI_PROVIDER_NOT_CONFIGURED", "message": "x"}
    fakes.replies = [gen(mc(0, **bad)), httpx.Response(503, json={"error": down})]
    resp = ask(api, count=1)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "AI_PROVIDER_NOT_CONFIGURED"


def test_a_failed_provider_on_a_retry_is_reported_not_rejected(api, fakes: Fakes):
    bad = {"evidence": "This sentence is nowhere in the passages."}
    failed = {"code": "AI_PROVIDER_FAILED", "message": "x"}
    fakes.replies = [gen(mc(0, **bad)), httpx.Response(502, json={"error": failed})]
    resp = ask(api, count=1)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "AI_PROVIDER_FAILED"


def test_an_unreadable_retry_with_nothing_kept_is_an_output_error(api, fakes: Fakes):
    bad = {"evidence": "This sentence is nowhere in the passages."}
    fakes.replies = [gen(mc(0, **bad)), httpx.Response(200, json={"output": "nope"})]
    resp = ask(api, count=1)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "AI_OUTPUT_REJECTED"


def test_a_retried_round_counts_what_was_sent_back(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1), mc(1)), rev("pass", WRONG_KEY), gen(mc(2), mc(3)), rev("pass", "pass")]
    first = ask(api, count=3).json()["rounds"][0]
    assert first["rejected"] == {"DUPLICATE": 1, "WRONG_KEY": 1} and first["sent_back"] == {"WRONG_KEY": 1}


def test_the_reviewer_is_shown_a_draft_it_skipped_earlier(api, fakes: Fakes):
    fakes.replies = [gen(mc(0), mc(1), mc(2)), rev("pass", WRONG_KEY), gen(mc(3)), rev("pass")]
    ask(api, count=3)
    second = task(sent(fakes, GEN, REV, GEN, REV)[3])
    assert second["existing_questions"] == [STEMS[0], STEMS[2]]
