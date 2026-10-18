"""The deterministic checks: what makes a generated question or drafted guidance acceptable, and every reason one is
dropped."""

import json
from typing import Any
from uuid import UUID

import pytest
from conftest import BOUNDARIES, CHUNK_1, CHUNK_2, DOC, ROOM, TOPIC, UNIT_TESTS

import checks
from contracts import GenerateRequest, GuidanceRequest
from prompts import Passage

PASSAGES = [
    Passage(1, CHUNK_1, DOC, "Testing slides", 3, UNIT_TESTS),
    Passage(2, CHUNK_2, DOC, "Testing slides", 5, BOUNDARIES),
]


def request(**change: Any) -> GenerateRequest:
    body: dict[str, Any] = {
        "classroom_id": ROOM,
        "document_ids": [DOC],
        "topic_id": TOPIC,
        "question_types": ["multiple_choice", "short_answer", "free_response"],
        "difficulty": "introductory",
        "count": 5,
        **change,
    }
    return GenerateRequest.model_validate(body)


def choice(**change: Any) -> dict[str, Any]:
    return {
        "type": "multiple_choice",
        "prompt": "What does a unit test check?",
        "options": ["A single function or class in isolation", "The whole system end to end"],
        "answer": "A single function or class in isolation",
        "accepted_answers": [],
        "criteria": [],
        "model_answer": "",
        "marking_points": "",
        "points": 1,
        "source_passages": [1],
        "evidence": "A unit test checks a single function or class in isolation.",
        "explanation": "The slides define a unit test this way.",
        **change,
    }


def short(**change: Any) -> dict[str, Any]:
    return choice(**{
        "type": "short_answer",
        "prompt": "What do we call a test that checks one function in isolation?",
        "options": [],
        "answer": "",
        "accepted_answers": ["unit test", "unit testing"],
        "marking_points": "Award the marks for 'unit test'. Do not accept 'integration test'.",
        **change,
    })  # fmt: skip


def written(**change: Any) -> dict[str, Any]:
    return choice(**{
        "type": "free_response",
        "prompt": "Using boundary value analysis, which ages would you test for a range of 18 to 65, and why?",
        "options": [],
        "answer": "",
        "criteria": [
            {"title": "Test values", "levels": [{"points": 2, "descriptor": "All four"},
                                                {"points": 0, "descriptor": "None"},
                                                {"points": 1, "descriptor": "Some"}]},
            {"title": "Reason", "levels": [{"points": 0, "descriptor": "None"},
                                           {"points": 2, "descriptor": "Off-by-one at the edges"}]},
        ],
        "model_answer": "17, 18, 65 and 66, because off-by-one mistakes happen at the edges of a range.",
        "marking_points": "One mark per correct value; two marks for explaining off-by-one errors.",
        "source_passages": [2],
        "evidence": "For a range of 18 to 65 inclusive, test 17, 18, 65 and 66",
        **change,
    })  # fmt: skip


def run(*items: dict[str, Any], **change: Any) -> tuple[list[Any], dict[str, int]]:
    kept, dropped = checks.questions(json.dumps({"questions": list(items)}), request(**change), PASSAGES)
    return kept, dict(dropped)


def test_each_type_becomes_a_draft_in_assessments_shape():
    kept, dropped = run(choice(), short(), written())
    assert dropped == {}
    mc, sa, fr = (g.question for g in kept)
    assert mc.options == ["A single function or class in isolation", "The whole system end to end"]
    assert (
        mc.answer == "A single function or class in isolation" and mc.points == 1 and mc.marking_scheme == []
    )
    assert sa.accepted_answers == ["unit test", "unit testing"] and sa.answer == ""
    assert (
        sa.marking_scheme[0].text.startswith("Award the marks") and sa.marking_scheme[0].document_id is None
    )
    assert fr.rubric is not None and fr.points == 4 and fr.max_length == 5000
    # Levels come back sorted, as Assessment stores them.
    assert [lv.points for lv in fr.rubric.criteria[0].levels] == [0, 1, 2]
    assert fr.sources[0].chunk_id == UUID(CHUNK_2) and fr.sources[0].page == 5
    assert all(g.question.topic_id == UUID(TOPIC) and g.question.origin == "generated" for g in kept)


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (choice(prompt="  "), "INVALID_PROMPT"),
        (choice(prompt="x" * 2001), "INVALID_PROMPT"),
        (choice(type="numerical"), "WRONG_TYPE"),
        (choice(source_passages=[]), "UNKNOWN_SOURCE"),
        (choice(source_passages=[3]), "UNKNOWN_SOURCE"),
        (choice(source_passages=[True]), "UNKNOWN_SOURCE"),
        (choice(evidence="A unit test checks everything at once."), "EVIDENCE_NOT_FOUND"),
        (choice(evidence="unit test"), "EVIDENCE_NOT_FOUND"),
        # Quoted, but from a passage the question does not cite.
        (choice(evidence="off-by-one mistakes happen at the edges"), "EVIDENCE_NOT_FOUND"),
        (choice(answer="Nothing"), "INVALID_KEY"),
        (choice(options=["Same", "Same"], answer="Same"), "INVALID_KEY"),
        (choice(options=["Only one"], answer="Only one"), "INVALID_KEY"),
        (short(accepted_answers=[]), "INVALID_KEY"),
        (short(accepted_answers=["Unit test", "unit  test"]), "INVALID_KEY"),
        (short(marking_points=""), "INVALID_GUIDANCE"),
        (short(marking_points="x" * 2001), "INVALID_GUIDANCE"),
        (written(criteria=[]), "INVALID_RUBRIC"),
        (written(model_answer=""), "INVALID_RUBRIC"),
        (
            written(
                criteria=[
                    {
                        "title": "T",
                        "levels": [{"points": 1, "descriptor": "a"}, {"points": 1, "descriptor": "b"}],
                    }
                ]
            ),
            "INVALID_RUBRIC",
        ),
        (
            written(
                criteria=[
                    {
                        "title": "T",
                        "levels": [{"points": 0, "descriptor": "a"}, {"points": 0, "descriptor": "b"}],
                    }
                ]
            ),
            "INVALID_RUBRIC",
        ),
        (written(criteria=[{"title": "T", "levels": [{"points": 0, "descriptor": "a"}]}]), "INVALID_RUBRIC"),
        (
            written(
                criteria=[
                    {
                        "title": "T",
                        "levels": [{"points": 0, "descriptor": "a"}, {"points": 101, "descriptor": "b"}],
                    }
                ]
            ),
            "INVALID_RUBRIC",
        ),
        # Each criterion is in range, but together they are worth more than Assessment allows.
        (
            written(
                criteria=[
                    {
                        "title": f"T{n}",
                        "levels": [{"points": 0, "descriptor": "a"}, {"points": 60, "descriptor": "b"}],
                    }
                    for n in range(2)
                ]
            ),
            "INVALID_RUBRIC",
        ),
    ],
)
def test_a_question_that_breaks_a_rule_is_dropped_with_its_reason(item, reason):
    kept, dropped = run(item, choice(prompt="Which test checks one class on its own?"))
    assert dropped == {reason: 1}
    assert [g.question.prompt for g in kept] == ["Which test checks one class on its own?"]


def test_evidence_survives_typographic_quotes_and_line_breaks():
    passages = [
        Passage(1, CHUNK_1, DOC, "Slides", 1, "Write the test first:\n“red, green, refactor” is the cycle.")
    ]
    item = choice(evidence='write the test first: "red, green,  refactor" is the cycle')
    kept, dropped = checks.questions(json.dumps({"questions": [item]}), request(), passages)
    assert len(kept) == 1 and not dropped


def test_near_duplicates_of_the_editors_questions_and_of_each_other_are_dropped():
    kept, dropped = run(
        choice(),
        choice(prompt="What does a unit test check ?"),
        short(),
        existing_prompts=["What do we call a test that checks one function in isolation"],
    )
    assert [g.question.type for g in kept] == ["multiple_choice"]
    assert dropped == {"DUPLICATE": 2}


def test_never_more_questions_than_asked_for():
    kept, dropped = run(choice(), short(), written(), count=2)
    assert len(kept) == 2 and not dropped


def test_only_the_asked_types():
    kept, dropped = run(choice(), short(), question_types=["short_answer"])
    assert [g.question.type for g in kept] == ["short_answer"] and dropped == {"WRONG_TYPE": 1}


@pytest.mark.parametrize("output", ["not json", "[]", '{"questions": "none"}'])
def test_an_unreadable_reply_is_rejected_whole(output):
    with pytest.raises(checks.UnreadableOutputError):
        checks.questions(output, request(), PASSAGES)


def test_at_most_five_sources_each_cut_to_assessments_limit():
    long = [Passage(n, CHUNK_1, DOC, "Slides", n, UNIT_TESTS + " x" * 1500) for n in range(1, 8)]
    kept, _ = checks.questions(
        json.dumps({"questions": [choice(source_passages=[1, 2, 3, 4, 5, 6, 7, 1])]}), request(), long
    )
    sources = kept[0].question.sources
    assert len(sources) == 5 and all(len(s.text) <= 2000 for s in sources)


def test_evidence_late_in_a_long_passage_is_stored_with_it():
    """The excerpt kept is the window around the evidence, so what the teacher sees holds the quote."""
    text = "Filler words. " * 300 + "\n" + UNIT_TESTS + " Trailing text." * 100
    kept, dropped = checks.questions(
        json.dumps({"questions": [choice()]}), request(), [Passage(1, CHUNK_1, DOC, "Slides", 1, text)]
    )
    assert not dropped
    [source] = kept[0].question.sources
    assert len(source.text) == 2000 and "checks a single function or class in isolation" in source.text


def test_evidence_only_in_a_passage_beyond_the_fifth_keeps_that_passage():
    others = [Passage(n, CHUNK_2, DOC, "Slides", n, BOUNDARIES) for n in range(1, 7)]
    holding = Passage(7, CHUNK_1, DOC, "Slides", 9, UNIT_TESTS)
    kept, dropped = checks.questions(
        json.dumps({"questions": [choice(source_passages=[1, 2, 3, 4, 5, 6, 7])]}),
        request(),
        [*others, holding],
    )
    assert not dropped
    sources = kept[0].question.sources
    assert len(sources) == 5 and sources[0].chunk_id == UUID(CHUNK_1) and sources[0].page == 9


def test_evidence_longer_than_an_excerpt_is_not_found():
    text = "word " * 1000
    item = choice(evidence=text.strip())
    kept, dropped = checks.questions(
        json.dumps({"questions": [item]}), request(), [Passage(1, CHUNK_1, DOC, "Slides", 1, text)]
    )
    assert not kept and dropped == {"EVIDENCE_NOT_FOUND": 1}


@pytest.mark.parametrize(
    "text", ["  Mixed\tCASE “quotes”\n\nand  ẞpaces ", "plain", "", " ", "a b"], ids=repr
)
def test_normalized_positions_match_normalize(text):
    folded, positions = checks._normalized_positions(text)
    assert folded == checks.normalize(text) and len(positions) == len(folded)


def guidance_request(kind: str) -> GuidanceRequest:
    return GuidanceRequest.model_validate(
        {"classroom_id": ROOM, "document_ids": [DOC], "prompt": "Which ages would you test?", "type": kind}
    )


def test_guidance_for_a_short_answer_and_a_free_response():
    sa = checks.guidance(json.dumps(short()), guidance_request("short_answer"), PASSAGES)
    assert sa.accepted_answers == ["unit test", "unit testing"] and sa.rubric is None
    assert sa.marking_points.text.startswith("Award") and sa.sources[0].chunk_id == UUID(CHUNK_1)
    fr = checks.guidance(json.dumps(written()), guidance_request("free_response"), PASSAGES)
    assert fr.accepted_answers == [] and fr.rubric is not None and len(fr.rubric.criteria) == 2


@pytest.mark.parametrize(
    ("item", "kind", "reason"),
    [
        (short(evidence="Unit tests are always fast and cheap."), "short_answer", "EVIDENCE_NOT_FOUND"),
        (short(accepted_answers=[]), "short_answer", "INVALID_KEY"),
        (written(model_answer=""), "free_response", "INVALID_RUBRIC"),
        (written(marking_points=""), "free_response", "INVALID_GUIDANCE"),
        (written(source_passages=[9]), "free_response", "UNKNOWN_SOURCE"),
    ],
)
def test_guidance_that_breaks_a_rule_is_refused(item, kind, reason):
    with pytest.raises(checks.RuleBrokenError) as refused:
        checks.guidance(json.dumps(item), guidance_request(kind), PASSAGES)
    assert refused.value.reason == reason
