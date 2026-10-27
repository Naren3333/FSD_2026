"""the generation eval holds drafts to the agent's own rules, scores the reviewer probe, and its case files are valid."""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import run_generation

DOC, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
CHUNK, FOREIGN = str(uuid.uuid4()), str(uuid.uuid4())
CHUNKS = {CHUNK: DOC, FOREIGN: OTHER}


def draft(
    prompt: str, answer: str = "Stub", options: list[str] | None = None, status: str = "passed"
) -> dict:
    return {
        "question": {
            "prompt": prompt,
            "type": "multiple_choice",
            "topic_id": str(uuid.uuid4()),
            "points": 1,
            "options": options or ["Stub", "Driver", "Mock", "Spy"],
            "answer": answer,
            "sources": [
                {"document_id": DOC, "chunk_id": CHUNK, "page": 3, "text": "A stub stands in for a callee."}
            ],
        },
        "evidence": "A stub stands in for a callee.",
        "explanation": "",
        "review": {"status": status, "note": "", "round": 0, "source": "reviewer"},
    }


def assessed(drafts: list[dict], status: int = 200, **run) -> dict:
    body = {
        "questions": drafts,
        "requested": 2,
        "returned": len(drafts),
        "rounds": [{"rejected": {"WRONG_KEY": 1}}],
        "model_calls": 2,
    }
    case = {
        "run": "x/review",
        "review": True,
        "counts": {"multiple_choice": 2},
        "document_ids": [DOC],
        "status": status,
        "body": body,
        "latency_ms": 1000,
        "cost_usd": 0.004,
        "generate_calls": 2,
        **run,
    }
    run_generation.assess(case, CHUNKS)
    return case


GOOD = [
    draft("What does a test stub stand in for?"),
    draft("Which double drives the unit under test?", "Driver"),
]


def test_grounded_distinct_drafts_pass():
    case = assessed(GOOD)
    assert case["failures"] == [] and case["flags"] == []
    assert case["digest"]["rejected"] == {"WRONG_KEY": 1} and case["digest"]["statuses"] == {"passed": 2}


def test_each_failure_is_caught():
    foreign = draft("Which tool records calls?", "Spy")
    foreign["question"]["sources"][0]["chunk_id"] = FOREIGN
    failures = assessed(
        [
            foreign,
            draft("According to the passage, what is a stub?"),
            draft("What does a test stub stand in for?"),
            draft("What does a test stub stand in for?"),
            draft("Which is a double?", answer="Fake"),
        ],
        existing=["What does a test stub stand in for?"],
    )["failures"]
    for expected in (
        "outside the chosen documents",
        "leans on the materials",
        "repeats another draft",
        "repeats existing",
        "not an option",
    ):
        assert any(expected in f for f in failures), expected


def test_too_many_calls_or_too_slow_fail_and_shortfalls_flag():
    case = assessed(GOOD[:1], latency_ms=301_000)
    case["body"]["model_calls"] = 7
    run_generation.assess(case, CHUNKS)
    assert any("7 model calls" in f for f in case["failures"])
    assert any("past kong's timeout" in f for f in case["failures"])
    assert any("short of {'multiple_choice': 1}" in f for f in case["flags"])


def test_long_keys_and_flagged_drafts_are_flagged():
    long = draft(
        "Which describes a stub?",
        "A stand-in that returns fixed answers to the unit under test",
        ["A stand-in that returns fixed answers to the unit under test", "A driver", "A spy", "A mock"],
        status="flagged",
    )
    flags = assessed([long, GOOD[0]])["flags"]
    assert any("long key" in f for f in flags) and any("1 flagged" in f for f in flags)


def test_a_refusal_must_make_no_generate_call():
    refusal = {"error": {"code": "INSUFFICIENT_SOURCE", "message": "too little"}}
    expect = {"status": 422, "code": "INSUFFICIENT_SOURCE"}
    case = {
        "run": "off/review",
        "review": True,
        "expect": expect,
        "status": 422,
        "body": refusal,
        "latency_ms": 5,
        "cost_usd": 0.0001,
    }
    ok = case | {"generate_calls": 0}
    run_generation.assess(ok, CHUNKS)
    assert ok["failures"] == []
    paid = case | {"generate_calls": 1}
    run_generation.assess(paid, CHUNKS)
    assert "should make none" in paid["failures"][0]


def test_runs_pair_review_on_and_off_unless_told_not_to():
    runs = run_generation.runs_of([{"id": "a"}, {"id": "b", "ab": False}], ab=True)
    assert [(r["run"], r["review"]) for r in runs] == [
        ("a/review", True),
        ("a/no-review", False),
        ("b/review", True),
    ]
    assert len(run_generation.runs_of([{"id": "a"}], ab=False)) == 1


def test_the_summary_compares_the_two_modes():
    on = assessed(GOOD)
    off = assessed(GOOD[:1], review=False, cost_usd=0.001)
    s = run_generation.summarize([on, off])
    assert s["modes"]["review"]["returned_rate"] == 1.0 and s["modes"]["no-review"]["returned_rate"] == 0.5
    assert s["review_minus_no_review"]["returned_rate"] == 0.5


def test_probe_scoring_counts_catches_names_and_false_rejects():
    batch = {
        "id": "b",
        "drafts": [
            {"id": "good", "expect": "pass"},
            {"id": "wrong", "expect": "fail", "reason": "WRONG_KEY"},
            {"id": "two", "expect": "fail", "reason": "AMBIGUOUS", "also": ["WRONG_KEY"]},
            {"id": "missed", "expect": "fail", "reason": "NOT_ANSWERABLE"},
            {"id": "skipped", "expect": "fail", "reason": "WEAK_DISTRACTORS"},
        ],
    }
    verdicts = [
        {"passed": False, "reason": "AMBIGUOUS", "note": "two keys"},
        {"passed": False, "reason": "WRONG_KEY", "note": ""},
        {"passed": False, "reason": "WRONG_KEY", "note": ""},
        {"passed": True, "reason": "NONE", "note": ""},
        {"passed": False, "reason": "NOT_REVIEWED", "note": ""},
    ]
    s = run_generation.probe_summary(run_generation.judge(batch, verdicts, 1))
    assert (
        s["not_reviewed"] == 1
        and s["catch_rate"] == round(2 / 3, 3)
        and s["exact_reason_rate"] == round(2 / 3, 3)
    )
    assert (
        s["false_reject_rate"] == 1.0
        and s["missed"] == ["missed"]
        and s["wrongly_failed"] == ["good (AMBIGUOUS)"]
    )


# ---- the case files


def test_generation_cases_name_corpus_documents_and_valid_types():
    titles = {
        m["title"]
        for m in run_generation.yaml.safe_load(
            Path(run_generation.ROOT / "scripts/eval/corpus.yaml").read_text(encoding="utf-8")
        ).values()
    }
    _, cases = run_generation.load_cases()
    assert len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        assert set(c["documents"]) <= titles, c["id"]
        run_generation.contracts.GenerateRequest.model_validate(
            {
                "classroom_id": str(uuid.uuid4()),
                "document_ids": [str(uuid.uuid4()) for _ in c["documents"]],
                "topic_id": str(uuid.uuid4()),
                "counts": c["counts"],
                "difficulty": c["difficulty"],
                "focus": c.get("focus", ""),
                "existing_prompts": c.get("existing", []),
            }
        )


def test_probe_drafts_are_valid_questions_with_known_reasons():
    reasons = set(run_generation.qg_prompts.REVIEW_REASONS) - {"NONE"}
    _, batches = run_generation.probe_batches()
    drafts = [d for b in batches for d in b["drafts"]]
    assert len({d["id"] for d in drafts}) == len(drafts)
    assert {d["expect"] for d in drafts} == {"pass", "fail"}
    for b in batches:
        for d in b["drafts"]:
            assert set(d["sources"]) <= set(b["passages"]), d["id"]
            if d["expect"] == "fail":
                assert {d["reason"], *d.get("also", [])} <= reasons, d["id"]
            # what probe_inside.py builds, so a bad draft fails here, not in the container.
            run_generation.contracts.DraftQuestion.model_validate(
                {
                    "prompt": d["prompt"],
                    "type": d["type"],
                    "topic_id": str(uuid.uuid4()),
                    "points": 1,
                    "options": d.get("options") or [],
                    "answer": d.get("answer") or "",
                    "accepted_answers": d.get("accepted") or [],
                    "sources": [{"text": b["passages"][s]} for s in d["sources"]],
                }
            )
            # the deterministic phrase check would stop it before the reviewer ever saw it.
            assert not run_generation.checks.NEEDS_MATERIALS_PATTERN.search(
                run_generation.checks.normalize(d["prompt"])
            ), d["id"]


@pytest.mark.parametrize(
    "reason",
    ["WRONG_KEY", "AMBIGUOUS", "NOT_ANSWERABLE", "NEEDS_MATERIALS", "WEAK_DISTRACTORS", "REPEATS_EXISTING"],
)
def test_the_probe_plants_every_reviewer_reason(reason):
    _, batches = run_generation.probe_batches()
    assert any(d.get("reason") == reason for b in batches for d in b["drafts"])
