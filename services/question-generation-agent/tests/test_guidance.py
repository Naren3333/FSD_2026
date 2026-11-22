"""marking guidance with its review (adr 0026): written, asked once more when it fails, reviewed on the check profile, and refused only after the one re-ask."""

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from conftest import CHUNK_2, SETTINGS, Fakes, as_user, reply
from test_api import GUIDANCE, GUIDANCE_BODY
from test_checks import written

import clients
import main
import prompts

WRITE, CHECK = "marking.guidance", "marking.guidance.reviewed"
BAD_EVIDENCE = {"evidence": "nothing like the slides at all, honestly"}


@pytest.fixture(autouse=True)
def reviewing(api) -> None:
    main.app.state.settings = replace(SETTINGS, review=True)


def verdict(passed: bool = True, reason: str = "NONE", note: str = "", **change: Any) -> httpx.Response:
    return reply({
        "own_answer": "17, 18, 65 and 66",
        "evidence_supports_key": True,
        "verdict": "pass" if passed else "fail",
        "reason": reason,
        "note": note,
        **change,
    })  # fmt: skip


WRONG = verdict(False, "WRONG_KEY", "The model answer misses 66.")


def draft(api) -> httpx.Response:
    return api.post(GUIDANCE, json=GUIDANCE_BODY, headers=as_user())


def sent(fakes: Fakes, *schemas: str) -> list[dict[str, Any]]:
    calls = fakes.calls("/v1/inference")
    assert [c["schema_name"] for c in calls] == list(schemas)
    assert all(c["billed_to"]["user_id"] == "t1" for c in calls)
    return calls


def test_guidance_the_reviewer_passes_is_two_calls(api, fakes: Fakes):
    fakes.replies = [reply(written()), verdict()]
    resp = draft(api)
    assert resp.status_code == 200, resp.text
    write, check = sent(fakes, WRITE, CHECK)
    assert write["model_profile"] == "default" and check["model_profile"] == "check"
    task = json.loads(check["messages"][1]["content"])
    # the reviewer sees the question, the key and only the cited passage, never the writer's evidence.
    assert task["question"] == GUIDANCE_BODY["prompt"] and task["guidance"]["rubric"]["model_answer"]
    assert [p["text"] for p in task["passages"]] == [fakes.results[1]["content"]]
    assert "evidence" not in task and CHUNK_2 not in check["messages"][1]["content"]
    assert check["messages"][0]["content"] == prompts.GUIDANCE_REVIEW_INSTRUCTIONS


def test_the_guidance_reviewer_runs_at_the_configured_effort(api, fakes: Fakes):
    """QUESTION_REVIEW_EFFORT overrides the check profile's effort for the review only (adr 0026)."""
    main.app.state.settings = replace(SETTINGS, review=True, review_effort="low")
    fakes.replies = [reply(written()), verdict()]
    assert draft(api).status_code == 200
    write, check = sent(fakes, WRITE, CHECK)
    assert "reasoning_effort" not in write and check["reasoning_effort"] == "low"


def test_guidance_the_reviewer_fails_is_written_again_and_reviewed_again(api, fakes: Fakes):
    fakes.replies = [reply(written()), WRONG, reply(written(model_answer="17, 18, 65 and 66.")), verdict()]
    resp = draft(api)
    assert resp.status_code == 200, resp.text
    assert resp.json()["rubric"]["model_answer"] == "17, 18, 65 and 66."
    _, _, again, _ = sent(fakes, WRITE, CHECK, WRITE, CHECK)
    # the rewrite carries the first draft and what the reviewer found.
    assert again["messages"][-2]["role"] == "assistant"
    assert prompts.PROBLEMS["WRONG_KEY"] in again["messages"][-1]["content"]
    assert "misses 66" in again["messages"][-1]["content"]


def test_guidance_failing_its_second_review_is_refused_after_four_calls(api, fakes: Fakes):
    fakes.replies = [reply(written()), WRONG, reply(written()), WRONG]
    resp = draft(api)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "GUIDANCE_REJECTED"
    assert len(sent(fakes, WRITE, CHECK, WRITE, CHECK)) == 4


def test_guidance_whose_passages_do_not_support_it_is_failed_even_on_a_pass(api, fakes: Fakes):
    unsupported = verdict(evidence_supports_key=False)
    fakes.replies = [reply(written()), unsupported, reply(written()), unsupported]
    resp = draft(api)
    assert resp.status_code == 422
    rewrite = sent(fakes, WRITE, CHECK, WRITE, CHECK)[2]
    assert prompts.KEY_UNSUPPORTED in rewrite["messages"][-1]["content"]


def test_a_re_ask_for_the_checks_uses_up_the_one_re_ask(api, fakes: Fakes):
    """write, re-ask, review: a failed review then has no re-ask left, so three calls at most."""
    fakes.replies = [reply(written(**BAD_EVIDENCE)), reply(written()), WRONG]
    resp = draft(api)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "GUIDANCE_REJECTED"
    sent(fakes, WRITE, WRITE, CHECK)


def test_a_rewrite_that_fails_the_checks_is_refused(api, fakes: Fakes):
    fakes.replies = [reply(written()), WRONG, reply(written(**BAD_EVIDENCE))]
    resp = draft(api)
    assert resp.status_code == 422
    sent(fakes, WRITE, CHECK, WRITE)


def test_guidance_still_unreadable_after_the_re_ask_is_an_output_error(api, fakes: Fakes):
    fakes.replies = [httpx.Response(200, json={"output": "nope"})] * 2
    resp = draft(api)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "AI_OUTPUT_REJECTED"
    sent(fakes, WRITE, WRITE)


@pytest.mark.parametrize(
    "review_reply",
    [
        httpx.Response(200, json={"output": "nope"}),
        httpx.Response(503, json={"error": {"code": "AI_UNAVAILABLE", "message": "x"}}),
    ],
    ids=["unreadable", "unavailable"],
)
def test_a_review_that_cannot_run_leaves_the_guidance_to_the_teacher(
    api, fakes: Fakes, review_reply: httpx.Response
):
    fakes.replies = [reply(written()), review_reply]
    resp = draft(api)
    assert resp.status_code == 200 and resp.json()["requires_teacher_review"] is True
    sent(fakes, WRITE, CHECK)


def test_a_rewrite_whose_review_cannot_be_read_is_not_served(api, fakes: Fakes):
    fakes.replies = [reply(written()), WRONG, reply(written()), httpx.Response(200, json={"output": "nope"})]
    assert draft(api).status_code == 422


def test_without_the_review_guidance_is_one_call(api, fakes: Fakes):
    main.app.state.settings = SETTINGS  # review off
    fakes.replies = [reply(written())]
    assert draft(api).status_code == 200
    sent(fakes, WRITE)


def test_guidance_calls_are_cut_off_at_the_deadline(api, fakes: Fakes):
    main.app.state.settings = replace(SETTINGS, review=True, review_deadline=100)
    fakes.replies = [reply(written()), WRONG, reply(written()), verdict()]
    assert draft(api).status_code == 200
    timeouts = [r.extensions["timeout"]["read"] for p, _, r in fakes.requests if p == "/v1/inference"]
    assert timeouts[0] == clients.GENERATE_TIMEOUT
    assert len(timeouts) == 4 and all(t <= 100 for t in timeouts[1:])
