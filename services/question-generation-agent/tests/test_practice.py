"""a student's own practice end to end against fakes (adr 0022): who may ask, what is searched with whose token, who pays, and that only reviewed questions reach the student."""

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from conftest import CHUNK_1, DOC, ROOM, SETTINGS, TOPIC, Fakes, as_user
from test_checks import choice, short
from test_review_loop import STEMS, WEAK, gen, rev, task

import main
import prompts

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
    assert task(write)["counts"] == {"multiple_choice": 1, "short_answer": 1}


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
