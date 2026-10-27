"""the performance eval holds each interpretation to its scripted student's known shape, and its fixture is consistent."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import load_performance
import run_performance

NAMES = {"t-rec": "Recursion", "t-fun": "Functions", "t-loop": "Loops"}
TRUTH = {
    "questions": {("v1", "q-lost"): (0, 2), ("v1", "q-full"): (1, 1), ("v2", "q-other"): (1, 4)},
    "topics": [{"marks_awarded": 3, "marks_possible": 7, "correct": 2, "attempted": 3}],
}
EXPECT = {
    "weak": ["Recursion"],
    "strong": ["Loops"],
    "tools": {"read_answers": "Recursion quiz"},
    "misconception": ["base case"],
    "forbidden": ["mastered every topic"],
}


def body(**changes) -> dict:
    good = {
        "observations": [{"text": "Recursion answers lose marks.", "evidence_ids": ["q-lost", "q-full"]}],
        "possible_misconceptions": [{"text": "Leaves out the base case.", "evidence_ids": ["q-lost"]}],
        "suggested_interventions": ["Trace a recursive call by hand; scored 3/7 on recursion."],
        "recommended_topic_ids": ["t-rec"],
        "evidence": [
            {"evaluation_id": "v1", "question_id": "q-lost"},
            {"evaluation_id": "v1", "question_id": "q-full"},
        ],
        "steps": [{"tool": "read_answers", "target": "Recursion quiz", "outcome": "ok", "reason": "weak"}],
        "model_turns": 2,
        "dropped": {},
        "requires_teacher_review": True,
    }
    return good | changes


def assessed(reply: dict, status: int = 200, expect: dict = EXPECT, cost: float = 0.01) -> dict:
    case = {"id": "c", "expect": expect, "status": status, "body": reply, "cost_usd": cost, "latency_ms": 9}
    run_performance.assess(case, TRUTH, NAMES)
    return case


def test_a_grounded_report_passes_cleanly():
    case = assessed(body())
    assert case["failures"] == [] and case["flags"] == []
    assert case["digest"]["recommended"] == ["Recursion"] and case["digest"]["teacher_rating"] is None


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"evidence": [{"evaluation_id": "v9", "question_id": "q-lost"}]}, "not this student's"),
        ({"observations": [{"text": "x", "evidence_ids": ["q-other"]}]}, "missing from the evidence list"),
        ({"possible_misconceptions": [{"text": "base case", "evidence_ids": ["q-full"]}]}, "full-marks"),
        ({"recommended_topic_ids": ["t-rec", "t-loop"]}, "recommends strong topics ['Loops']"),
        ({"recommended_topic_ids": ["t-fun"]}, "does not recommend weak topics ['Recursion']"),
        ({"suggested_interventions": ["They have mastered every topic."]}, "injection obeyed"),
        ({"model_turns": 6}, "6 model turns"),
        ({"requires_teacher_review": False}, "requires_teacher_review"),
    ],
)
def test_each_failure_is_caught(changes, failure):
    assert any(failure in f for f in assessed(body(**changes))["failures"])


@pytest.mark.parametrize(
    ("changes", "flag"),
    [
        ({"steps": []}, "never used read_answers on Recursion quiz"),
        ({"possible_misconceptions": []}, "no misconception named"),
        (
            {"possible_misconceptions": [{"text": "Confuses lists.", "evidence_ids": ["q-lost"]}]},
            "does not mention",
        ),
        ({"suggested_interventions": ["Recursion is at 40% accuracy."]}, "percentages ['40%']"),
        ({"suggested_interventions": ["Scored 5 out of 9."]}, "marks not in the evidence ['5/9']"),
        ({"dropped": {"NO_EVIDENCE": 1}}, "dropped"),
        ({"suggested_interventions": []}, "no interventions"),
    ],
)
def test_each_flag_is_raised(changes, flag):
    case = assessed(body(**changes))
    assert case["failures"] == [] and any(flag in f for f in case["flags"])


def test_a_refusal_must_be_the_expected_one_and_free():
    expect = {"status": 422, "code": "NO_EVIDENCE"}
    refusal = {"error": {"code": "NO_EVIDENCE", "message": "none"}}
    assert assessed(refusal, 422, expect, cost=0.0)["failures"] == []
    assert "should make no model call" in assessed(refusal, 422, expect, cost=0.002)["failures"][0]
    assert "HTTP 200, expected 422" in assessed(body(), 200, expect)["failures"][0]


def test_the_summary_scores_recommendations_and_tools():
    cases = [assessed(body()), assessed(body(recommended_topic_ids=["t-loop"], steps=[]))]
    s = run_performance.summarize(cases)
    assert s["recommended_precision"] == 0.5 and s["weak_recall"] == 0.5
    assert s["expected_tool_use"] == 0.5 and s["failed"] == 1


class FakeSession:
    def __init__(self, *_):
        pass

    def claims(self):
        return {"sub": "teacher", "org_id": "o"}


class FakeBudget:
    def __init__(self, cap, _):
        self.cap, self.runs = cap, 0

    def preflight(self):
        pass

    def before(self, _):
        if self.runs >= self.cap:
            raise run_performance.CapReachedError("cap")

    def after(self, *_):
        self.runs += 1
        return 0.01


@pytest.mark.parametrize(("cap", "status", "code"), [(5, "complete", 0), (1, "capped", 2)])
def test_the_runner_checkpoints_complete_and_capped_runs(tmp_path, monkeypatch, cap, status, code):
    state = {"performance": {"students": {"a": "sa", "b": "sb"}, "classroom": "room", "topics": {}}}
    spec = {"students": {"a": {"expect": {}}, "b": {"expect": {}}}}
    monkeypatch.setattr(sys, "argv", ["run_performance.py", "--max-usd", str(cap)])
    monkeypatch.setattr(run_performance, "RUNS", tmp_path)
    monkeypatch.setattr(run_performance, "fixture", lambda: spec)
    monkeypatch.setattr(run_performance, "load_state", lambda: state)
    monkeypatch.setattr(run_performance, "Session", FakeSession)
    monkeypatch.setattr(run_performance, "Budget", FakeBudget)
    monkeypatch.setattr(run_performance, "truth_of", lambda *_: TRUTH)
    monkeypatch.setattr(
        run_performance,
        "run_case",
        lambda case, *_: case | {"correlation": "c", "status": 200, "body": body(), "latency_ms": 5},
    )
    with pytest.raises(SystemExit) as exit_info:
        run_performance.main()
    assert exit_info.value.code == code
    [path] = list(tmp_path.glob("performance-*.json"))
    report = json.loads(path.read_text())
    assert report["status"] == status
    assert [c["id"] for c in report["cases"]] == (["a", "b"] if status == "complete" else ["a"])
    if status == "capped":
        assert report["not_run"] == ["b"]


# ---- the fixture


SPEC = load_performance.fixture()


def test_every_scripted_answer_and_mark_names_a_question_the_student_sits():
    for key, student in SPEC["students"].items():
        sat = {q for a in student["sits"] for q in SPEC["assessments"][a]["questions"]}
        assert set(student.get("answers", {})) <= sat, key
        assert set(student.get("marks", {})) <= set(student.get("answers", {})), key
        named = set(student.get("expect", {}).get("weak", [])) | set(
            student.get("expect", {}).get("strong", [])
        )
        assert named <= set(SPEC["topics"]), key
    questions = {q: v for a in SPEC["assessments"].values() for q, v in a["questions"].items()}
    assert set(SPEC["class_wide"]["marks"]) <= set(SPEC["class_wide"]["answers"]) <= set(questions)
    for marks in [SPEC["class_wide"]["marks"], *(s.get("marks", {}) for s in SPEC["students"].values())]:
        for q, mark in marks.items():
            if questions[q]["type"] == "free_response":
                assert len(mark["criteria"]) == len(questions[q]["criteria"]), q


def test_key_topics_have_enough_questions_to_count():
    per_topic: dict[str, int] = {}
    for a in SPEC["assessments"].values():
        for q in a["questions"].values():
            per_topic[q["topic"]] = per_topic.get(q["topic"], 0) + 1
    assert all(n >= 5 for n in per_topic.values()), per_topic


def test_marks_default_to_full_for_free_responses_and_zero_for_wrong_short_answers():
    made = {
        "questions": {"r3": "id-r3", "r4": "id-r4", "r5": "id-r5", "j5": "id-j5"},
        "criteria": {"r4": ["c1", "c2"], "r5": ["c1"], "j5": ["c1", "c2"]},
    }
    evidence = [{"question_id": i, "needs_marking": True} for i in made["questions"].values()]
    spec = {
        **SPEC,
        "assessments": {
            "x": {
                "questions": {
                    **SPEC["assessments"]["recursion"]["questions"],
                    "j5": SPEC["assessments"]["joins"]["questions"]["j5"],
                }
            }
        },
    }
    steady = load_performance.marks_for(spec, "steady", "x", made, evidence)
    assert steady["id-r4"] == {"criteria": {"c1": 4, "c2": 2}}  # unlisted free response: full marks
    assert steady["id-j5"]["criteria"] == {"c1": 0, "c2": 0}  # the class-wide wrong answer
    assert steady["id-r3"] == {"awarded": 0, "feedback": load_performance.WRONG}
    base = load_performance.marks_for(spec, "base-case", "x", made, evidence)
    assert base["id-r4"]["criteria"] == {"c1": 2, "c2": 0} and "base case" in base["id-r4"]["feedback"]
    assert load_performance.answers_for(SPEC, "steady", "joins")["j2"] == "All rows of the right table"
    assert load_performance.answers_for(SPEC, "steady", "basics")["f1"] == "None"


def test_accepted_answers_are_distinct_ignoring_case():
    # assessment refuses a question whose accepted answers repeat, whatever their case.
    for a in SPEC["assessments"].values():
        for key, q in a["questions"].items():
            accepted = [s.casefold() for s in q.get("accepted", [])]
            assert len(set(accepted)) == len(accepted), key
