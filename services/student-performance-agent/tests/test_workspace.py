"""what the model is shown stays within ai-core's message size however much evidence a student has."""

import json
from typing import Any

from conftest import QUIZ, QUIZ_ASSESSMENT, finalized

import prompts
from workspace import MAX_READ, OPENING_BUDGET, Workspace


def test_the_opening_leaves_out_the_oldest_results_when_they_do_not_fit():
    many = [finalized(id=f"e{i}", evidence=finalized()["evidence"] * 15) for i in range(20)]
    ws = Workspace()
    ws.add_results(many, {QUIZ: QUIZ_ASSESSMENT})
    ws.fit()
    opening = ws.opening()
    assert len(json.dumps(opening)) <= OPENING_BUDGET
    assert 1 <= len(opening["results"]) < 20 and opening["results"][0]["result"] == "R1"
    # a result left out of the opening cannot be read either.
    assert ws.result("R20") is None


def test_reading_answers_shows_lost_marks_first_and_at_most_a_few():
    ws = Workspace()
    ws.add_results([finalized(evidence=finalized()["evidence"] * 5)], {QUIZ: QUIZ_ASSESSMENT})
    shown = ws.read_answers(ws.results[0], None)
    assert len(shown["questions"]) == MAX_READ and shown["not_shown"] == 15 - MAX_READ
    assert [q["marks"] for q in shown["questions"][:5]] == ["0/2"] * 5


def test_old_tool_results_are_trimmed_but_the_calls_stay():
    history = [{"call": {"tool": "read_answers"}, "output": {"text": "x" * 15000}} for _ in range(4)]
    kept = prompts.fitting(history)
    assert sum(len(json.dumps(h)) for h in kept) <= prompts.HISTORY_BUDGET
    assert kept[0]["output"] == "(trimmed to save space)" and kept[-1] == history[-1]
    assert all(h["call"] == {"tool": "read_answers"} for h in kept)


def test_answers_trimmed_from_the_task_no_longer_count_as_read():
    """a misconception may only cite answers the reporting turn can still see."""
    ws = Workspace()
    ws.add_results([finalized()], {QUIZ: QUIZ_ASSESSMENT})
    read = {"call": {"tool": "read_answers"}, "output": ws.read_answers(ws.results[0], None)}
    refs = {q["evidence"] for q in read["output"]["questions"]}
    assert prompts.shown(prompts.messages(ws.opening(), [read], 2, 5, False)) == refs
    filler = [{"call": {"tool": "class_comparison"}, "output": {"text": "x" * 15000}} for _ in range(3)]
    assert prompts.shown(prompts.messages(ws.opening(), [read, *filler], 5, 5, True)) == set()


def test_the_opening_is_fitted_once_so_later_topics_never_drop_a_result():
    many = [finalized(id=f"e{i}", evidence=finalized()["evidence"] * 15) for i in range(20)]
    ws = Workspace()
    ws.add_results(many, {QUIZ: QUIZ_ASSESSMENT})
    ws.fit()
    kept = len(ws.results)
    for i in range(30):
        ws.topic(f"prerequisite-{i}", "A prerequisite with a long name " * 4)
    assert len(ws.opening()["results"]) == kept and ws.result(f"R{kept}") is not None


def test_the_task_stays_whole_json_when_the_opening_grows_past_its_budget():
    """topics added by tools can grow the opening; the history then gives way so the message is never cut."""
    ws = Workspace()
    ws.add_results([finalized()], {QUIZ: QUIZ_ASSESSMENT})
    for i in range(150):
        ws.topic(f"prerequisite-{i}", "A prerequisite topic with a long name " * 10)
    history = [{"call": {"tool": "read_answers"}, "output": {"text": "x" * 15000}} for _ in range(4)]
    _, user = prompts.messages(ws.opening(), history, 3, 5, False)
    assert len(user["content"]) <= prompts.MAX_MESSAGE
    assert json.loads(user["content"])["tool_history"][-1]["call"] == {"tool": "read_answers"}


def test_too_many_or_no_references_fall_back_to_plain_strings():
    many = [f"E{n}" for n in range(1, prompts.MAX_ENUM + 2)]
    assert prompts.refs(many)["items"] == {"type": "string"}
    assert prompts.refs([])["items"] == {"type": "string"}
    assert prompts.refs(["E1"])["items"] == {"type": "string", "enum": ["E1"]}
    # the report alone on the last turn, inside a turn otherwise; both stay strict.
    assert prompts.schema(True, ["E1"], ["T1"])["additionalProperties"] is False
    assert set(prompts.schema(False, ["E1"], ["T1"])["properties"]) == {"action", "tool_calls", "report"}


def test_prerequisite_and_class_checks_are_suggested_until_made():
    """the opening shows no prerequisite links or class figures, so the task names the checks that could change the report."""

    def opening(marks: dict[str, list[str]]) -> dict[str, Any]:
        questions = [{"topic": t, "marks": m} for t, ms in marks.items() for m in ms]
        return {"results": [{"result": "R1", "questions": questions}]}

    def calls(task: list[dict[str, str]]) -> list[tuple[str, str | None]]:
        return [(c["tool"], c["topic"]) for c in json.loads(task[-1]["content"])["suggested_checks"]]

    # one slip, nothing weak: no checks.
    assert calls(prompts.messages(opening({"T1": ["0/2", "2/2"], "T2": ["1/1"]}), [], 1, 5, False)) == []
    # one weak topic: the class, not prerequisites.
    one = opening({"T1": ["0/2", "1/2"], "T2": ["0/1"]})
    assert calls(prompts.messages(one, [], 1, 5, False)) == [("class_comparison", None)]
    # two weak topics: either may build on the other.
    two = opening({"T1": ["0/2", "1/2"], "T2": ["0/1", "0/1", "1/1"]})
    assert calls(prompts.messages(two, [], 1, 5, False)) == [
        ("prerequisites", "T1"),
        ("prerequisites", "T2"),
        ("class_comparison", None),
    ]
    # an answered call is no longer suggested; a failed one still is.
    history = [
        {"call": {"tool": "prerequisites", "result": None, "topic": "T1"}, "output": {"prerequisites": []}},
        {
            "call": {"tool": "class_comparison", "result": None, "topic": "T2"},
            "output": {"error": "No time."},
        },
    ]
    assert calls(prompts.messages(two, history, 2, 5, False)) == [
        ("prerequisites", "T2"),
        ("class_comparison", None),
    ]
    # the last turn may not call tools, so it suggests none.
    assert "suggested_checks" not in json.loads(prompts.messages(two, [], 5, 5, True)[-1]["content"])
