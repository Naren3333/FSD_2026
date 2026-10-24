"""what the generator and the reviewer are asked."""

import json
from typing import Any
from uuid import uuid4

from conftest import BOUNDARIES, CHUNK_1, CHUNK_2, DOC, UNIT_TESTS
from test_checks import PASSAGES, choice, request

import checks
import prompts
from contracts import Generated
from prompts import MAX_MESSAGE, Passage

STEMS = [
    "What does a unit test check?",
    "Why are dependencies replaced with test doubles?",
    "Which values sit at the edges of an input range?",
    "Name one place where off-by-one mistakes happen.",
    "How does a failing unit test help a developer?",
    "When is a payment service replaced in a test?",
    "Which ages does boundary analysis pick for 18 to 65?",
    "Explain what isolation means for a class under test.",
    "What would an integration test check instead?",
    "Describe the purpose of testing edge values.",
]


def drafts(items: list[dict[str, Any]], passages: list[Passage] = PASSAGES) -> list[Generated]:
    kept, _, _, _ = checks.questions(
        json.dumps({"questions": items}), request(counts={"multiple_choice": 10}), passages
    )
    return kept


def task_of(messages: list[dict[str, str]]) -> dict[str, Any]:
    return json.loads(messages[1]["content"])


def test_review_message_holds_no_evidence_explanation_or_ids():
    held = drafts([choice(explanation="Zebra-striped reasoning for the teacher.")])
    messages = prompts.review_messages("Testing", "introductory", held, PASSAGES, ["Existing one?"])
    text = messages[1]["content"]
    # the evidence is a quote from the passage, so only its key can be absent.
    assert "Zebra" not in text and "evidence" not in text and "explanation" not in text
    assert CHUNK_1 not in text and CHUNK_2 not in text and DOC not in text
    task = task_of(messages)
    assert task["existing_questions"] == ["Existing one?"]
    assert set(task["questions"][0]) == {
        "number",
        "type",
        "prompt",
        "options",
        "answer",
        "accepted_answers",
        "rubric",
        "passages",
    }


def test_a_draft_gets_only_the_passages_it_cites():
    held = drafts(
        [
            choice(
                prompt="Which values does boundary analysis test?",
                options=["The edges of each range", "Only the middle"],
                answer="The edges of each range",
                source_passages=[2],
                evidence="test 17, 18, 65 and 66",
            ),
            choice(),
        ]
    )
    task = task_of(prompts.review_messages("T", "introductory", held, PASSAGES, []))
    assert [q["number"] for q in task["questions"]] == [1, 2]
    assert [p["text"] for p in task["questions"][0]["passages"]] == [BOUNDARIES]
    assert [p["text"] for p in task["questions"][1]["passages"]] == [UNIT_TESTS]


def test_a_review_of_ten_long_drafts_and_fifty_long_prompts_fits_one_message():
    passages = [
        Passage(i, str(uuid4()), DOC, "Slides", i, "Filler. " * 600 + f"Fact number {i} is stated here.")
        for i in range(1, 6)
    ]
    assert all(len(p.text) > 4500 for p in passages)
    items = [
        choice(
            prompt=STEMS[n],
            source_passages=[1, 2, 3, 4, 5],
            evidence="Fact number 1 is stated here.",
        )
        for n in range(10)
    ]
    held = drafts(items, passages)
    assert len(held) == 10
    existing = [f"Existing question {n}. " + "x" * 1900 for n in range(50)]
    messages = prompts.review_messages("T", "introductory", held, passages, existing)
    assert len(messages[1]["content"]) <= MAX_MESSAGE
    task = task_of(messages)
    assert len(task["questions"]) == 10 and task["existing_questions"]


def test_the_task_asks_for_a_count_of_each_type():
    body = request(counts={"multiple_choice": 3, "short_answer": 2})
    task = task_of(prompts.generation_messages("T", body, PASSAGES))
    assert task["counts"] == {"multiple_choice": 3, "short_answer": 2}
    assert "count" not in task and "question_types" not in task


def test_generation_without_attempts_is_unchanged():
    body = request(existing_prompts=["Old one?"])
    plain = prompts.generation_messages("T", body, PASSAGES)
    assert plain == prompts.generation_messages("T", body, PASSAGES, ())
    assert "previous_attempts" not in plain[1]["content"]
    assert plain[0]["content"] == prompts.GENERATION_INSTRUCTIONS


def test_generation_with_attempts_lists_them_with_the_problem_in_words():
    attempts = [
        checks.Rejected("Bad key?", "multiple_choice", "WRONG_KEY"),
        checks.Rejected("No proof?", "short_answer", "EVIDENCE_NOT_FOUND"),
    ]
    messages = prompts.generation_messages("T", request(existing_prompts=["Old one?"]), PASSAGES, attempts)
    task = task_of(messages)
    assert task["previous_attempts"] == [
        {"prompt": "Bad key?", "type": "multiple_choice", "problem": prompts.PROBLEMS["WRONG_KEY"]},
        {"prompt": "No proof?", "type": "short_answer", "problem": prompts.PROBLEMS["EVIDENCE_NOT_FOUND"]},
    ]
    assert task["existing_questions"] == ["Old one?"]
    assert messages[0]["content"].startswith(prompts.GENERATION_INSTRUCTIONS)
    assert "previous_attempts lists questions that were rejected" in messages[0]["content"]


def test_attempts_drop_the_oldest_when_they_do_not_fit():
    attempts = [checks.Rejected(f"{n} " + "y" * 1900, "multiple_choice", "AMBIGUOUS") for n in range(200)]
    messages = prompts.generation_messages("T", request(), PASSAGES, attempts)
    assert len(messages[1]["content"]) <= MAX_MESSAGE
    kept = task_of(messages)["previous_attempts"]
    assert kept and kept[-1]["prompt"].startswith("199 ") and not kept[0]["prompt"].startswith("0 ")


def test_every_reason_the_loop_feeds_back_has_words():
    reasons = [r for r in prompts.REVIEW_REASONS if r != "NONE"] + ["NOT_REVIEWED"]
    assert all(r in prompts.PROBLEMS for r in reasons)
    assert all(
        getattr(checks, r) in prompts.PROBLEMS
        for r in ("INVALID_PROMPT", "UNKNOWN_SOURCE", "EVIDENCE_NOT_FOUND", "INVALID_KEY", "INVALID_RUBRIC")
    )


def test_review_schema_requires_every_property():
    schema = prompts.review_schema()
    item = schema["properties"]["reviews"]["items"]
    assert item["required"] == ["question", "own_answer", "verdict", "reason", "note"]
    assert item["additionalProperties"] is False


def test_the_reviewer_may_fail_a_draft_for_needing_the_materials():
    enum = prompts.review_schema()["properties"]["reviews"]["items"]["properties"]["reason"]["enum"]
    assert "NEEDS_MATERIALS" in enum
    for phrase in (
        "NEEDS_MATERIALS",
        "absolute word",
        "even in another type",
        "described as",
        "fail only the later one",
    ):
        assert phrase in prompts.REVIEW_INSTRUCTIONS


def test_the_generator_is_told_to_stand_alone_and_keep_options_alike():
    for phrase in ("never seen the materials", "similar in length", "defining words", "different fact"):
        assert phrase in prompts.GENERATION_INSTRUCTIONS
