"""the marking eval scores each suggestion against its drafted answer's expected levels, and its fixture is consistent."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import load_marking
import run_marking

SPEC = load_marking.fixture()
FLAGS = {"blank", "off_topic", "possible_instructions_in_answer", "language_mismatch"}


def short(answer: str) -> str:
    """assessment's own short-answer rule (grading's normalizeShort), case ignored."""
    return " ".join(answer.split()).lower()


def test_every_case_expects_a_level_of_each_criterion():
    ids = [c["id"] for c in SPEC["cases"]]
    assert len(ids) == len(set(ids)) and 45 <= len(ids) <= 60
    for c in SPEC["cases"]:
        q = SPEC["questions"][c["question"]]
        levels = (
            [[p for p, _ in k["levels"]] for k in q["criteria"]] if q["type"] == "free_response" else [[0, 1]]
        )
        assert len(c["expect"]) == len(levels), c["id"]
        assert all(e in ls for e, ls in zip(c["expect"], levels, strict=True)), c["id"]
        assert "teacher_rating" in c and c["teacher_rating"] is None, c["id"]
        assert set(c.get("flags") or []) <= FLAGS, c["id"]
        assert c["answer"].strip(), c["id"]


def test_auto_and_exact_cases_are_what_they_claim():
    for c in SPEC["cases"]:
        q = SPEC["questions"][c["question"]]
        if q["type"] != "short_answer":
            assert not c.get("auto") and not c.get("exact"), c["id"]
            continue
        matched = short(c["answer"]) in {short(a) for a in q["accepted"]}
        # an auto case is marked by assessment's rule; every other short answer reaches the marker.
        assert matched == bool(c.get("auto")), c["id"]
        if c.get("exact"):
            assert c["expect"] == [1], c["id"]
    kinds = {k for c in SPEC["cases"] for k in ("auto", "exact", "flags") if c.get(k)}
    assert kinds == {"auto", "exact", "flags"}


def test_no_student_sits_a_question_twice():
    who = load_marking.assign(SPEC)
    pairs = [(c["question"], who[c["id"]]) for c in SPEC["cases"]]
    assert len(pairs) == len(set(pairs))
    assert {who[c["id"]] for c in SPEC["cases"]} == {
        load_marking.username(n) for n in range(1, load_marking.students_needed(SPEC) + 1)
    }


def test_question_inputs_follow_the_assessment_contract():
    race = load_marking.question_input(SPEC["questions"]["race"], "t1")
    assert race["rubric"]["criteria"][0]["levels"][2] == {
        "points": 4,
        "descriptor": "Explains that both threads read the same value before either writes, so one increment is lost",
    }
    assert race["rubric"]["model_answer"] and "accepted_answers" not in race
    tdd = load_marking.question_input(SPEC["questions"]["tdd"], "t2")
    assert tdd["accepted_answers"] == ["Green", "green phase"] and tdd["case_sensitive"] is False
    assert "marking_scheme" not in tdd
    bigo = load_marking.question_input(SPEC["questions"]["bigo"], "t3")
    assert [set(e) for e in bigo["marking_scheme"]] == [{"text"}]
    assert "Do not accept" in bigo["marking_scheme"][0]["text"]


def test_the_scheme_question_has_cases_it_rules_out_and_accepts():
    # each excerpt fits assessment's limits: 1-5 passages of 1-2000 characters.
    for q in SPEC["questions"].values():
        scheme = q.get("marking_scheme") or []
        assert len(scheme) <= 5 and all(0 < len(t) <= 2000 for t in scheme)
    expected = {c["id"]: c["expect"] for c in SPEC["cases"] if c["question"] == "bigo"}
    assert expected["bigo-logarithmic"] == [0] and expected["bigo-notation-and-words"] == [1]


def test_the_harder_set_needs_one_student_per_case_of_the_busiest_question():
    # race has the most cases, 17, and each student sits its assessment once.
    assert load_marking.students_needed(SPEC) == 17
    assert {c["question"] for c in SPEC["cases"]} == set(SPEC["questions"])


IDS = ["c1", "c2"]


def suggestion(
    points: list[int],
    review: str | None = None,
    critic: list[int] | None = None,
    ids: list[str] = IDS,
    **extra,
) -> dict:
    s = {
        "status": "ready",
        "awarded": sum(points),
        "criteria": [
            {"criterion_id": i, "points": p, "rationale": "r"} for i, p in zip(ids, points, strict=True)
        ],
        "confidence": "high",
        "flags": [],
        "model": "m",
    } | extra
    if review:
        s["review"] = {"status": review, "awarded": sum(critic or [])}
        if critic is not None:
            s["review"]["criteria"] = [
                {"criterion_id": i, "points": p, "rationale": "r"} for i, p in zip(ids, critic, strict=True)
            ]
    return s


def case(expect: list[int], evidence: dict | None, status: int = 201, ids: list[str] = IDS, **extra) -> dict:
    c = {
        "id": "x",
        "answer": "a",
        "expect": expect,
        "criteria": ids,
        "status": status,
        "evidence": evidence,
        "cost_usd": 0.01,
        "latency_ms": 10,
        "teacher_rating": None,
    } | extra
    run_marking.assess(c)
    return c


def needs(s: dict | None) -> dict:
    return {"question_id": "q", "needs_marking": True, "awarded": 0, "possible": 6, "suggestion": s}


def test_a_matching_suggestion_agrees():
    c = case([4, 2], needs(suggestion([4, 2], "agreed", [4, 2])))
    assert c["failures"] == [] and c["agrees"] and c["critic_agrees"] and c["outcome"] == "ready"
    assert c["digest"]["first"] == [4, 2] and c["digest"]["teacher_rating"] is None


@pytest.mark.parametrize(
    ("evidence", "status", "extra", "failure"),
    [
        (None, 409, {}, "submission HTTP 409"),
        (needs({"status": "pending", "awarded": 0, "reason": "AI_SPEND_LIMIT"}), 201, {}, "(AI_SPEND_LIMIT)"),
        (needs(suggestion([4, 2], "pending")), 201, {}, "timed out"),
        (
            needs(suggestion([1], ids=["match"])),
            201,
            {"exact": True, "ids": ["match"]},
            "expected an exact-match",
        ),
        (
            needs(suggestion([1], ids=["match"])),
            201,
            {"auto": True, "ids": ["match"]},
            "expected assessment's rule",
        ),
    ],
)
def test_each_failure_is_caught(evidence, status, extra, failure):
    ids = extra.pop("ids", IDS)
    c = case([1] if ids == ["match"] else [4, 2], evidence, status, ids, **extra)
    assert any(failure in f for f in c["failures"]), c["failures"]


def test_auto_and_exact_outcomes():
    auto = case(
        [1], {"question_id": "q", "awarded": 1, "possible": 1, "correct": True}, ids=["match"], auto=True
    )
    assert auto["outcome"] == "auto" and auto["first"] == [1] and auto["agrees"] and auto["failures"] == []
    s = suggestion([1], "not_reviewed", ids=["match"], model="exact_match")
    exact = case([1], needs(s), ids=["match"], exact=True)
    assert exact["outcome"] == "exact" and exact["agrees"] and exact["review"] == "not_reviewed"


def test_the_summary_measures_agreement_and_the_critic():
    cases = [
        # right, and the critic agrees.
        case([4, 2], needs(suggestion([4, 2], "agreed", [4, 2]))),
        # wrong by 2 points on one criterion; the critic doubts it, with the expected levels.
        case([2, 1], needs(suggestion([4, 1], "doubtful", [2, 1]))),
        # wrong by 3 points on both; the critic agrees with it (a missed doubt).
        case([0, 0], needs(suggestion([2, 1], "agreed", [2, 1]))),
        # right, but the critic doubts it (a false doubt).
        case([4, 0], needs(suggestion([4, 0], "doubtful", [2, 0]))),
        # the reply quoted words not in the answer.
        case([0, 0], needs({"status": "failed", "awarded": 0, "reason": "UNGROUNDED"})),
        # the injection was flagged, the one it asked for was not missed.
        case(
            [0, 0],
            needs(suggestion([0, 0], "agreed", [0, 0], flags=["possible_instructions_in_answer"])),
            flags=["possible_instructions_in_answer"],
        ),
        case([0, 0], needs(suggestion([0, 0], "agreed", [0, 0])), flags=["possible_instructions_in_answer"]),
    ]
    s = run_marking.summarize(cases)
    assert s["cases"] == 7 and s["failed"] == 0
    assert s["outcomes"] == {"failed": 1, "ready": 6}
    # 12 criteria marked, 9 at the expected level; 4 of 6 cases wholly right; points off by 0, 2, 3, 0, 0, 0.
    assert (
        s["level_agreement"] == 0.75 and s["case_agreement"] == 0.667 and s["model_case_agreement"] == 0.667
    )
    assert s["mean_abs_points_error"] == round(5 / 6, 3)
    assert s["unusable_rate"] == round(1 / 7, 3) and s["unusable_reasons"] == {"UNGROUNDED": 1}
    assert s["critic_doubtful_rate"] == round(2 / 6, 3)
    assert s["critic_true_doubt_rate"] == 0.5 and s["critic_false_doubt_rate"] == 0.25
    assert s["critic_case_agreement"] == round(4 / 6, 3)
    assert s["critic_wrong_cases"] == 2
    assert s["expected_flags_raised"] == 0.5
    assert s["cost_usd"] == 0.07


def test_an_empty_run_has_no_rates():
    s = run_marking.summarize([])
    assert (
        s["level_agreement"] is None and s["critic_true_doubt_rate"] is None and s["latency_ms_p50"] is None
    )


def test_the_cost_is_charged_to_the_budget():
    budget = run_marking.Budget(0.05, "t")
    run_marking.charge(budget, 0.03)
    assert budget.spent == 0.03 and budget.estimate(run_marking.KIND) == 0.03
    with pytest.raises(run_marking.CapReachedError):
        budget.before(run_marking.KIND)
