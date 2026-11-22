"""a student's own practice end to end against fakes (adr 0022): who may ask, what is searched with whose token, who pays, and that only reviewed questions reach the student."""

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from conftest import CHUNK_1, DOC, ROOM, SETTINGS, TOPIC, Fakes, as_user, reply
from test_checks import choice, short
from test_review_loop import (
    GEN,
    KEY_HOLDS,
    REV,
    STEMS,
    UNREADABLE,
    WEAK,
    WRONG_KEY,
    ask,
    gen,
    rev,
    sent,
    task,
)

import main
import pipeline
import prompts
from contracts import MAX_COUNT

PRACTICE = "/v1/practice"
BODY: dict[str, Any] = {
    "classroom_id": ROOM,
    "document_ids": [DOC],
    "topic_id": TOPIC,
    "counts": {"multiple_choice": 1, "short_answer": 1},
    "difficulty": "introductory",
    "focus": "unit tests",
}
STUDENT_PAYS = {"user_id": "s1", "org_id": "org", "role": "student"}


@pytest.fixture(autouse=True)
def reviewing(api) -> None:
    configure()


def configure(**change: Any) -> None:
    main.app.state.settings = replace(SETTINGS, **{"review": True, **change})


def practise(api, user: str = "s1", **change: Any) -> httpx.Response:
    return api.post(PRACTICE, json={**BODY, **change}, headers=as_user(user))


def calls(fakes: Fakes, *schemas: str) -> list[dict[str, Any]]:
    """the ai-core calls, in order, each billed to the student."""
    sent = fakes.calls("/v1/inference")
    assert [c["schema_name"] for c in sent] == list(schemas)
    assert all(c["billed_to"] == STUDENT_PAYS for c in sent)
    return sent


def test_a_student_gets_reviewed_questions_with_their_keys(api, fakes: Fakes):
    fakes.replies = [gen(choice(), short()), rev("pass", "pass")]
    resp = practise(api)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["requested"] == 2 and data["returned"] == 2 and data["dropped"] == {}
    assert data["model_calls"] == 2
    mc, sa = data["questions"]
    assert mc == {
        "prompt": "What does a unit test check?",
        "type": "multiple_choice",
        "options": ["A single function or class in isolation", "The whole system end to end"],
        "answer": "A single function or class in isolation",
        "accepted_answers": [],
        "explanation": "The slides define a unit test this way.",
        "evidence": "A unit test checks a single function or class in isolation.",
        "sources": [
            {"document_id": DOC, "chunk_id": CHUNK_1, "page": 3, "text": fakes.results[0]["content"]}
        ],
        "topic_id": TOPIC,
    }
    # no marks or marking points: nothing is marked.
    assert sa["accepted_answers"] == ["unit test", "unit testing"] and sa["answer"] == ""
    assert "points" not in sa and "marking_scheme" not in sa and "requires_teacher_review" not in data
    # the student's own token searches the chosen teaching material for the topic and focus.
    (search,) = fakes.calls("/v1/search")
    assert fakes.headers("/v1/search")[0]["authorization"] == "Bearer s1"
    assert fakes.headers("/v1/topics/" + TOPIC)[0]["authorization"] == "Bearer s1"
    assert search["query"] == "Software testing. unit tests" and search["doc_types"] == ["teaching_material"]
    # the student's own allowance pays, under practice's own names, with practice instructions.
    write, check = calls(fakes, "practice.generated", "practice.reviewed")
    assert write["messages"][0]["content"] == prompts.PRACTICE_GENERATION_INSTRUCTIONS
    assert check["messages"][0]["content"] == prompts.PRACTICE_REVIEW_INSTRUCTIONS
    # one spare multiple choice is asked for; short answers are asked for as requested.
    assert task(write)["counts"] == {"multiple_choice": 2, "short_answer": 1}


@pytest.mark.parametrize("user", ["t1", "s2", "a1"])
def test_only_a_student_enrolled_in_the_classroom_may_practise(api, fakes: Fakes, user):
    """a teacher (even of this classroom), a student of another classroom and a service account are refused."""
    resp = practise(api, user)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN"
    assert not fakes.calls("/v1/search") and not fakes.calls("/v1/inference")


def test_a_teacher_token_enrolled_as_a_student_is_refused(api, fakes: Fakes):
    fakes.students.add(("Bearer t1", ROOM))
    fakes.teachers.clear()
    assert practise(api, "t1").status_code == 403


def test_a_student_cannot_use_the_teachers_routes(api, fakes: Fakes):
    teacher_body = {**BODY, "counts": {"multiple_choice": 1}}
    assert api.post("/v1/questions", json=teacher_body, headers=as_user("s1")).status_code == 403
    assert not fakes.requests


@pytest.mark.parametrize(
    "change",
    [
        {"counts": {"free_response": 1}},
        {"counts": {"multiple_choice": 1, "free_response": 1}},
        {"counts": {}},
        {"counts": {"multiple_choice": 6, "short_answer": 5}},
        {"document_ids": []},
        {"document_ids": [DOC, DOC]},
        {"document_ids": [DOC] * 11},
        {"focus": "x" * 301},
        {"existing_prompts": ["q"] * 51},
        {"review": False},  # a student cannot skip the review
    ],
)
def test_requests_outside_the_contract(api, fakes: Fakes, change):
    assert practise(api, **change).status_code == 422
    assert not fakes.requests


def test_a_focus_without_a_topic_is_the_query(api, fakes: Fakes):
    fakes.replies = [gen(choice()), rev("pass")]
    body = {**BODY, "counts": {"multiple_choice": 1}, "focus": "isolation"}
    del body["topic_id"]
    resp = api.post(PRACTICE, json=body, headers=as_user("s1"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["questions"][0]["topic_id"] is None
    assert fakes.calls("/v1/search")[0]["query"] == "isolation"
    assert not fakes.calls("/v1/topics/" + TOPIC) and not fakes.calls("/v1/documents/page")
    # the nil id the pipeline runs under never reaches the model.
    write = calls(fakes, "practice.generated", "practice.reviewed")[0]
    assert task(write)["topic"] == "" and "00000000-0000-0000-0000-000000000000" not in json.dumps(write)


def test_with_neither_topic_nor_focus_the_documents_titles_are_the_query(api, fakes: Fakes):
    other = "00000000-0000-4000-8000-0000000000d2"
    fakes.documents.append({"id": other, "classroom_id": ROOM, "title": "Boundary notes"})
    fakes.replies = [gen(choice()), rev("pass")]
    resp = practise(api, topic_id=None, focus="", document_ids=[other, DOC], counts={"multiple_choice": 1})
    assert resp.status_code == 200, resp.text
    assert fakes.calls("/v1/search")[0]["query"] == "Boundary notes. Testing slides"
    assert fakes.headers("/v1/documents/page")[0]["authorization"] == "Bearer s1"


def test_titles_are_found_past_the_first_page_of_documents(api, fakes: Fakes):
    """content pages its list at 100: a chosen document further down is still found, and paging stops there."""
    fakes.documents = [{"id": f"00000000-0000-4000-8000-{n:012d}", "title": f"Deck {n}"} for n in range(250)]
    fakes.documents[150] = {"id": DOC, "title": "Testing slides"}
    fakes.replies = [gen(choice()), rev("pass")]
    resp = practise(api, topic_id=None, focus="", counts={"multiple_choice": 1})
    assert resp.status_code == 200, resp.text
    assert fakes.calls("/v1/search")[0]["query"] == "Testing slides"
    assert len(fakes.headers("/v1/documents/page")) == 2


class Shrinking(list[dict[str, Any]]):
    """a document list whose first document is deleted right after the first page is read."""

    shrunk = False

    def __getitem__(self, index: Any) -> Any:
        page = super().__getitem__(index)
        if isinstance(index, slice) and not self.shrunk:
            self.shrunk = True
            del self[0]
        return page


def test_a_document_deleted_between_pages_does_not_hide_a_chosen_one(api, fakes: Fakes):
    """the chosen document moves from the second page's first place to the first page's last: the pages overlap, so it is still read."""
    documents = [{"id": f"00000000-0000-4000-8000-{n:012d}", "title": f"Deck {n}"} for n in range(150)]
    documents[100] = {"id": DOC, "title": "Testing slides"}
    fakes.documents = Shrinking(documents)
    fakes.replies = [gen(choice()), rev("pass")]
    resp = practise(api, topic_id=None, focus="", counts={"multiple_choice": 1})
    assert resp.status_code == 200, resp.text
    assert fakes.calls("/v1/search")[0]["query"] == "Testing slides"


def test_documents_the_student_cannot_see_say_too_little(api, fakes: Fakes):
    fakes.documents = []
    resp = practise(api, topic_id=None, focus="")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "INSUFFICIENT_SOURCE"
    assert not fakes.calls("/v1/search") and not fakes.calls("/v1/inference")


def test_a_malformed_document_list_is_a_search_failure(api, fakes: Fakes):
    fakes.documents = [{"title": "no id"}]
    resp = practise(api, topic_id=None, focus="")
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "SEARCH_UNAVAILABLE"


def test_an_unknown_topic(api, fakes: Fakes):
    fakes.topic = None
    resp = practise(api)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "UNKNOWN_TOPIC"
    assert not fakes.calls("/v1/inference")


def test_flagged_and_unreviewed_drafts_never_reach_the_student(api, fakes: Fakes):
    """no teacher reviews practice: a weak draft and one the reviewer gave no verdict on are dropped and counted."""
    configure(review_retries=0)
    fakes.replies = [
        gen(choice(prompt=STEMS[0]), choice(prompt=STEMS[1]), choice(prompt=STEMS[2])),
        rev("pass", WEAK),  # no verdict on the third
    ]
    resp = practise(api, counts={"multiple_choice": 3})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [q["prompt"] for q in data["questions"]] == [STEMS[0]]
    assert data["returned"] == 1 and data["dropped"] == {"WEAK_DISTRACTORS": 1, "NOT_REVIEWED": 1}


def test_a_weak_draft_with_a_verified_key_is_still_rewritten_for_a_student(api, fakes: Fakes):
    """unlike a teacher's request, practice spends a round to replace it, told which options were weak (adr 0022)."""
    named = {"weak_options": ["The whole system end to end"]}
    fakes.replies = [
        gen(choice(prompt=STEMS[0])),
        rev((*WEAK, named)),
        gen(choice(prompt=STEMS[1])),
        rev("pass"),
    ]
    resp = practise(api, counts={"multiple_choice": 1})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [q["prompt"] for q in data["questions"]] == [STEMS[1]]
    assert data["dropped"] == {"WEAK_DISTRACTORS": 1} and data["model_calls"] == 4
    _, _, rewrite, _ = calls(
        fakes, "practice.generated", "practice.reviewed", "practice.generated", "practice.reviewed"
    )
    (attempt,) = task(rewrite)["previous_attempts"]
    assert attempt["prompt"] == STEMS[0] and attempt["weak_options"] == named["weak_options"]


def test_an_unreviewed_draft_does_not_fill_its_place(api, fakes: Fakes):
    """a retry is paid to replace a draft the reviewer did not judge, which a teacher's request would keep; the unjudged draft is not held against its rewrite."""
    fakes.replies = [gen(choice(prompt=STEMS[0])), rev(), gen(choice(prompt=STEMS[1])), rev("pass")]
    resp = practise(api, counts={"multiple_choice": 1})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [q["prompt"] for q in data["questions"]] == [STEMS[1]]
    assert data["dropped"] == {"NOT_REVIEWED": 1} and data["model_calls"] == 4
    _, _, rewrite, recheck = calls(
        fakes, "practice.generated", "practice.reviewed", "practice.generated", "practice.reviewed"
    )
    assert STEMS[0] not in task(rewrite)["existing_questions"]
    assert STEMS[0] not in task(recheck)["existing_questions"]


def test_a_review_that_cannot_run_returns_nothing_unreviewed(api, fakes: Fakes):
    configure(review_retries=0)
    failed = httpx.Response(502, json={"error": {"code": "AI_PROVIDER_FAILED", "message": "x"}})
    fakes.replies = [gen(choice()), failed]
    resp = practise(api, counts={"multiple_choice": 1})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "AI_PROVIDER_FAILED"


@pytest.mark.parametrize(
    ("change", "review_reply"),
    [
        ({"review_deadline": 0}, None),  # past the deadline the review is not started
        ({}, httpx.Response(200, json={"output": "nope"})),  # an unreadable review
        ({}, rev()),  # a review with no verdicts
    ],
    ids=["out of time", "unreadable", "no verdict"],
)
def test_a_review_that_judged_nothing_is_not_called_a_rejection(
    api, fakes: Fakes, change: dict[str, Any], review_reply: httpx.Response | None
):
    """nothing was refused, so the student is told to try again, not to choose other materials."""
    configure(review_retries=0, **change)
    fakes.replies = [gen(choice())] + ([review_reply] if review_reply else [])
    resp = practise(api, counts={"multiple_choice": 1})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "REVIEW_UNAVAILABLE"


def test_when_nothing_passes_the_student_is_told_in_plain_words(api, fakes: Fakes):
    configure(review_retries=0)
    fakes.replies = [gen(choice()), rev(("WRONG_KEY", "The key is wrong."))]
    resp = practise(api, counts={"multiple_choice": 1})
    assert resp.status_code == 422
    assert resp.json()["error"] == {
        "code": "GENERATION_REJECTED",
        "message": "None of the practice questions passed the checks. Try other materials or a different focus.",
    }


def test_with_the_review_switched_off_practice_is_unavailable(api, fakes: Fakes):
    configure(review=False)
    resp = practise(api)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "AI_UNAVAILABLE"
    assert not fakes.calls("/v1/search") and not fakes.calls("/v1/inference")


def test_the_spend_limit_names_the_students_reset(api, fakes: Fakes):
    error = {"code": "AI_SPEND_LIMIT", "message": "x", "resets_at": "2026-10-18T00:00:00+08:00"}
    fakes.replies = [httpx.Response(429, json={"error": error})]
    resp = practise(api)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "AI_SPEND_LIMIT"
    calls(fakes, "practice.generated")


def test_questions_the_student_already_has_are_not_repeated(api, fakes: Fakes):
    configure(review_retries=0)
    fakes.replies = [gen(choice(prompt=STEMS[0]), choice(prompt=STEMS[1])), rev("pass")]
    resp = practise(api, counts={"multiple_choice": 2}, existing_prompts=[STEMS[0]])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [q["prompt"] for q in data["questions"]] == [STEMS[1]] and data["dropped"] == {"DUPLICATE": 1}
    assert task(calls(fakes, "practice.generated", "practice.reviewed")[0])["existing_questions"] == [
        STEMS[0]
    ]


def test_every_practice_change_rewrites_a_teacher_rule():
    """the practice instructions are the teacher's rules with named lines changed; a renamed rule must not slip through unchanged."""
    for instructions, changes in (
        (prompts.GENERATION_INSTRUCTIONS, prompts.PRACTICE_GENERATION_CHANGES),
        (prompts.REVIEW_INSTRUCTIONS, prompts.PRACTICE_REVIEW_CHANGES),
    ):
        lines = instructions.split("\n")
        assert all(any(line.startswith(key) for line in lines) for key in changes)
    assert (
        "free_response" not in prompts.PRACTICE_GENERATION_INSTRUCTIONS.split("Rules:")[1].split("points:")[0]
    )
    # past the opening line, which says no teacher checks them, nothing is written for a teacher.
    for practice in (prompts.PRACTICE_GENERATION_INSTRUCTIONS, prompts.PRACTICE_REVIEW_INSTRUCTIONS):
        assert "teacher" not in practice.split("\n", 1)[1]


# ---- independent review and re-asks (adr 0026)


def test_practice_is_reviewed_on_the_check_profile(api, fakes: Fakes):
    fakes.replies = [gen(choice(), short()), rev("pass", "pass")]
    assert practise(api).status_code == 200
    write, check = calls(fakes, "practice.generated", "practice.reviewed")
    assert write["model_profile"] == "default" and check["model_profile"] == "check"


def test_a_practice_key_the_passages_do_not_support_never_reaches_the_student(api, fakes: Fakes):
    configure(review_retries=0)
    unsupported = {"question": 1, **KEY_HOLDS, "evidence_supports_key": False}
    fakes.replies = [
        gen(choice()),
        reply({"reviews": [{**unsupported, "verdict": "pass", "reason": "NONE", "note": ""}]}),
    ]
    resp = practise(api, counts={"multiple_choice": 1})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "GENERATION_REJECTED"


def test_an_unreadable_practice_write_is_asked_again_on_the_students_allowance(api, fakes: Fakes):
    fakes.replies = [UNREADABLE, gen(choice()), rev("pass")]
    resp = practise(api, counts={"multiple_choice": 1})
    assert resp.status_code == 200 and resp.json()["model_calls"] == 3
    calls(fakes, "practice.generated", "practice.generated", "practice.reviewed")


# ---- spare multiple choice (adr 0022)


def test_extra_multiple_choice_is_asked_for_and_the_first_passes_served(api, fakes: Fakes):
    """three wanted, five asked for: every one passes, the first three in review order are served, and the two spare passes are not counted as dropped."""
    fakes.replies = [gen(*(choice(prompt=STEMS[i]) for i in range(5))), rev(*["pass"] * 5)]
    resp = practise(api, counts={"multiple_choice": 3})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [q["prompt"] for q in data["questions"]] == STEMS[:3]
    assert data["requested"] == 3 and data["returned"] == 3 and data["dropped"] == {}
    write, check = calls(fakes, "practice.generated", "practice.reviewed")
    assert task(write)["counts"] == {"multiple_choice": 5} and len(task(check)["questions"]) == 5


def test_a_spare_pass_fills_a_weak_drafts_place_without_another_round(api, fakes: Fakes):
    fakes.replies = [
        gen(choice(prompt=STEMS[0]), choice(prompt=STEMS[1]), choice(prompt=STEMS[2])),
        rev("pass", WEAK, "pass"),
    ]
    resp = practise(api, counts={"multiple_choice": 2})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [q["prompt"] for q in data["questions"]] == [STEMS[0], STEMS[2]]
    assert data["dropped"] == {"WEAK_DISTRACTORS": 1} and data["model_calls"] == 2


def test_a_later_round_asks_for_the_shortfall_and_one_more(api, fakes: Fakes):
    fakes.replies = [
        gen(choice(prompt=STEMS[0]), choice(prompt=STEMS[1]), short()),
        rev(WEAK, WEAK, "pass"),
        gen(choice(prompt=STEMS[2]), choice(prompt=STEMS[3])),
        rev("pass", "pass"),
    ]
    resp = practise(api)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [q["prompt"] for q in data["questions"]] == [STEMS[2], short()["prompt"]]
    first, _, retry, _ = calls(
        fakes, "practice.generated", "practice.reviewed", "practice.generated", "practice.reviewed"
    )
    assert task(first)["counts"] == {"multiple_choice": 2, "short_answer": 1}
    # the short answer is full, so only multiple choice is asked for again; the spare pass is not dropped.
    assert task(retry)["counts"] == {"multiple_choice": 2} and data["dropped"] == {"WEAK_DISTRACTORS": 2}


def test_spare_passes_are_existing_questions_for_the_next_round(api, fakes: Fakes):
    """a short answer still wanted: the spare multiple choice that passed is not written again."""
    again = "Name a test that checks one class alone."
    fakes.replies = [
        gen(choice(prompt=STEMS[0]), choice(prompt=STEMS[1]), short()),
        rev("pass", "pass", WRONG_KEY),
        gen(short(prompt=again)),
        rev("pass"),
    ]
    resp = practise(api)
    assert resp.status_code == 200, resp.text
    assert [q["prompt"] for q in resp.json()["questions"]] == [STEMS[0], again]
    schemas = ("practice.generated", "practice.reviewed") * 2
    retry = calls(fakes, *schemas)[2]
    assert task(retry)["counts"] == {"short_answer": 1}
    assert {STEMS[0], STEMS[1]} <= set(task(retry)["existing_questions"])


def test_a_teachers_request_asks_for_exactly_its_counts(api, fakes: Fakes):
    """no spare drafts for a teacher: the reply's fourth multiple choice is not read."""
    fakes.replies = [gen(*(choice(prompt=STEMS[i]) for i in range(4))), rev("pass", "pass", "pass")]
    resp = ask(api, count=3)
    assert resp.status_code == 200, resp.text
    write, check = sent(fakes, GEN, REV)
    assert task(write)["counts"] == {"multiple_choice": 3} and len(task(check)["questions"]) == 3
    assert write["messages"][0]["content"] == prompts.GENERATION_INSTRUCTIONS


@pytest.mark.parametrize(
    ("needed", "first", "want"),
    [
        ({"multiple_choice": 1}, True, {"multiple_choice": 2}),
        ({"multiple_choice": 3}, True, {"multiple_choice": 5}),
        ({"multiple_choice": 4, "short_answer": 2}, True, {"multiple_choice": 6, "short_answer": 2}),
        ({"multiple_choice": 5, "short_answer": 2}, True, {"multiple_choice": 6, "short_answer": 2}),
        ({"multiple_choice": 8}, True, {"multiple_choice": 8}),
        ({"multiple_choice": 10}, True, {"multiple_choice": 10}),
        ({"multiple_choice": 3}, False, {"multiple_choice": 4}),
        ({"multiple_choice": 2, "short_answer": 6}, False, {"multiple_choice": 2, "short_answer": 6}),
        ({"short_answer": 3}, True, {"short_answer": 3}),
    ],
)
def test_the_extra_drafts_asked_for(needed: dict[str, int], first: bool, want: dict[str, int]):
    assert pipeline.asked(needed, first) == want


def test_a_write_is_never_asked_for_more_drafts_than_a_request_may_hold():
    """every practice request stays within ASK_MAX (or what was requested) and MAX_COUNT, short answers as requested."""
    for choices in range(MAX_COUNT + 1):
        for shorts in range(MAX_COUNT + 1 - choices):
            needed = {k: v for k, v in (("multiple_choice", choices), ("short_answer", shorts)) if v}
            for first in (True, False):
                got = pipeline.asked(needed, first)
                assert sum(got.values()) <= max(pipeline.ASK_MAX, choices + shorts) <= MAX_COUNT
                assert got.get("short_answer", 0) == shorts and got.get("multiple_choice", 0) >= choices


def test_spare_drafts_keep_to_the_call_budget(api, fakes: Fakes):
    """every round fails: three writes and three reviews, as before."""
    fakes.replies = [
        gen(choice(prompt=STEMS[0]), choice(prompt=STEMS[1])),
        rev(WRONG_KEY, WRONG_KEY),
        gen(choice(prompt=STEMS[2]), choice(prompt=STEMS[3])),
        rev(WRONG_KEY, WRONG_KEY),
        gen(choice(prompt=STEMS[4]), choice(prompt=STEMS[5])),
        rev(WRONG_KEY, WRONG_KEY),
    ]
    resp = practise(api, counts={"multiple_choice": 1})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "GENERATION_REJECTED"
    assert len(fakes.calls("/v1/inference")) == pipeline.MAX_CALLS
