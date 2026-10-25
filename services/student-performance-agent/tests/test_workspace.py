"""what the model is shown stays within ai-core's message size however much evidence a student has."""

import json

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
    assert ws.read == {q["evidence"] for q in shown["questions"]}


def test_old_tool_results_are_trimmed_but_the_calls_stay():
    history = [{"call": {"tool": "read_answers"}, "output": {"text": "x" * 15000}} for _ in range(4)]
    kept = prompts.fitting(history)
    assert sum(len(json.dumps(h)) for h in kept) <= prompts.HISTORY_BUDGET
    assert kept[0]["output"] == "(trimmed to save space)" and kept[-1] == history[-1]
    assert all(h["call"] == {"tool": "read_answers"} for h in kept)


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
