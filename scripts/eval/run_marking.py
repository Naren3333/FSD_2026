"""run the marking eval (adr 0024): grading's marker and critic on real submissions; --rescore re-reads a run."""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

from budget import DEFAULT_CAP, Budget, CapReachedError, spent_since
from common import RUNS, Session, load_state
from load_marking import SECTION, assign, fixture
from local_stack import check
from reporting import checkpoint, ratio, select_cases

KIND = "marking"
WAIT = 420  # seconds for a suggestion and its second check: two calls of up to 90 s each, with a re-ask
POLL = 3
# suggestion reasons that mean the model's reply could not be used.
UNUSABLE = {"UNGROUNDED", "INVALID_OUTPUT", "CONTRADICTED", "AI_OUTPUT_REJECTED", "EMPTY_OUTPUT"}


def load_cases(spec: dict, state: dict) -> list[dict]:
    part, who = state[SECTION], assign(spec)
    cases = []
    for c in spec["cases"]:
        made = part["assessments"][c["question"]]
        cases.append(
            {
                **c,
                "user": who[c["id"]],
                "student_id": part["students"][who[c["id"]]],
                "assessment_id": made["id"],
                "question_id": made["question"],
                "criteria": made["criteria"],
            }
        )
    return cases


def submitted(student: Session, case: dict) -> bool:
    return (
        student.request("GET", f"/assessment/assessments/{case['assessment_id']}/submission").status_code
        == 200
    )


def evidence_of(teacher: Session, room: str, case: dict) -> dict | None:
    """the case's question in its student's grade, or none before grading has the submission."""
    page = check(
        teacher.request("GET", f"/grading/evaluations?classroom_id={room}&student_id={case['student_id']}")
    ).json()
    for v in page["evaluations"]:
        if v["assessment_id"] == case["assessment_id"]:
            return next((e for e in v["evidence"] if e["question_id"] == case["question_id"]), None)
    return None


def settled(evidence: dict | None) -> bool:
    """marked by assessment's rule, or a suggestion that is ready or failed with no second check still to come."""
    if evidence is None:
        return False
    if not evidence.get("needs_marking"):
        return True
    s = evidence.get("suggestion") or {}
    if s.get("status") in (None, "pending"):
        return False
    return (s.get("review") or {}).get("status") != "pending"


def wait_for(teacher: Session, room: str, case: dict) -> dict | None:
    deadline = time.monotonic() + WAIT
    while True:
        evidence = evidence_of(teacher, room, case)
        if settled(evidence) or time.monotonic() > deadline:
            return evidence
        time.sleep(POLL)


def run_case(case: dict, teacher: Session, room: str, state: dict, teacher_id: str) -> dict:
    """submits the answer and waits; the case's cost is what the teacher was charged meanwhile."""
    student = Session(case["user"], state)
    since = datetime.now(UTC)
    started = time.monotonic()
    path = f"/assessment/assessments/{case['assessment_id']}/submissions"
    res = student.request("POST", path, json={"answers": {case["question_id"]: case["answer"]}})
    evidence = wait_for(teacher, room, case) if res.status_code in (200, 201) else None
    return {
        **case,
        "status": res.status_code,
        "evidence": evidence,
        "cost_usd": spent_since(teacher_id, since) if res.status_code in (200, 201) else 0.0,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def levels(criteria: list[dict] | None, ids: list[str]) -> list[int | None]:
    """a marking's points for each criterion, in rubric order; none for a criterion it did not mark."""
    by_id = {c["criterion_id"]: c["points"] for c in criteria or []}
    return [by_id.get(i) for i in ids]


def assess(case: dict) -> None:
    """adds the first marking's and the critic's levels, whether each matches `expect`, and `failures`."""
    e, expect = case.get("evidence"), case["expect"]
    failures: list[str] = []
    s = (e or {}).get("suggestion") or {}
    review = s.get("review") or {}
    case["outcome"] = outcome(case)
    if case["status"] not in (200, 201):
        failures.append(f"submission HTTP {case['status']}")
    elif not settled(e):
        failures.append(
            "timed out waiting for the suggestion" + (f" ({s.get('reason')})" if s.get("reason") else "")
        )
    elif case.get("auto") and case["outcome"] != "auto":
        failures.append(f"expected assessment's rule to mark it, got {case['outcome']}")
    elif case.get("exact") and case["outcome"] != "exact":
        failures.append(f"expected an exact-match suggestion, got {case['outcome']}")
    first = levels(s.get("criteria"), case["criteria"]) if s.get("status") == "ready" else None
    second = (
        levels(review.get("criteria"), case["criteria"])
        if review.get("status") in ("agreed", "doubtful")
        else None
    )
    if case["outcome"] == "auto":
        first = [e["awarded"]] if e else None
    case["first"], case["second"] = first, second
    case["agrees"] = first == expect if first is not None else None
    case["critic_agrees"] = second == expect if second is not None else None
    case["review"] = review.get("status")
    case["missing_flags"] = [f for f in case.get("flags") or [] if f not in (s.get("flags") or [])]
    case["failures"] = failures
    case["digest"] = {
        "answer": case["answer"],
        "expect": expect,
        "first": first,
        "second": second,
        "outcome": case["outcome"],
        "reason": s.get("reason"),
        "review": review.get("status"),
        "review_reasons": review.get("reasons") or [],
        "confidence": s.get("confidence"),
        "flags": s.get("flags") or [],
        "rationales": [c.get("rationale") for c in s.get("criteria") or []],
        "cost_usd": case["cost_usd"],
        "latency_ms": case["latency_ms"],
        "teacher_rating": case.get("teacher_rating"),
    }


def outcome(case: dict) -> str:
    """auto (assessment's rule marked it), exact (grading's exact match), ready, failed, pending or missing."""
    e = case.get("evidence")
    if not e:
        return "missing"
    if not e.get("needs_marking"):
        return "auto"
    s = e.get("suggestion") or {}
    if s.get("status") == "ready" and s.get("model") == "exact_match":
        return "exact"
    return str(s.get("status") or "pending")


def summarize(cases: list[dict]) -> dict:
    modelled = [c for c in cases if c["outcome"] in ("ready", "failed")]
    marked = [c for c in cases if c["first"] is not None and c["outcome"] != "auto"]
    modelled_ready = [c for c in marked if c["outcome"] == "ready"]
    criteria = [(a, b) for c in marked for a, b in zip(c["first"], c["expect"], strict=True)]
    reviewed = [c for c in marked if c["review"] in ("agreed", "doubtful")]
    wrong = [c for c in reviewed if not c["agrees"]]
    right = [c for c in reviewed if c["agrees"]]
    reasons: dict[str, int] = {}
    for c in modelled:
        if c["outcome"] == "failed":
            reason = c["digest"]["reason"] or "?"
            reasons[reason] = reasons.get(reason, 0) + 1
    flagged = [c for c in cases if c.get("flags")]
    lat = sorted(c["latency_ms"] for c in cases if c["status"] in (200, 201))
    return {
        "cases": len(cases),
        "failed": sum(bool(c["failures"]) for c in cases),
        "outcomes": {o: sum(c["outcome"] == o for c in cases) for o in sorted({c["outcome"] for c in cases})},
        # the first marking against the expected levels: every criterion, whole cases, and total points.
        "level_agreement": ratio(sum(a == b for a, b in criteria), len(criteria)),
        "case_agreement": ratio(sum(bool(c["agrees"]) for c in marked), len(marked)),
        "model_case_agreement": ratio(sum(bool(c["agrees"]) for c in modelled_ready), len(modelled_ready)),
        "mean_abs_points_error": ratio(
            sum(abs(total(c["first"]) - total(c["expect"])) for c in marked), len(marked)
        ),
        # replies that could not be used, of the cases that needed a model.
        "unusable_rate": ratio(sum(c["outcome"] == "failed" for c in modelled), len(modelled)),
        "unusable_reasons": reasons,
        "critic_doubtful_rate": ratio(sum(c["review"] == "doubtful" for c in reviewed), len(reviewed)),
        # doubtful when the first marking is wrong (higher is better), and when it is right (lower is better).
        "critic_true_doubt_rate": ratio(sum(c["review"] == "doubtful" for c in wrong), len(wrong)),
        "critic_false_doubt_rate": ratio(sum(c["review"] == "doubtful" for c in right), len(right)),
        "critic_case_agreement": ratio(sum(bool(c["critic_agrees"]) for c in reviewed), len(reviewed)),
        "critic_wrong_cases": sum(c["critic_agrees"] is False for c in reviewed),
        "not_reviewed": sum(c["review"] == "not_reviewed" for c in marked),
        "expected_flags_raised": ratio(sum(not c["missing_flags"] for c in flagged), len(flagged)),
        "cost_usd": round(sum(c["cost_usd"] for c in cases), 6),
        "cost_usd_max": max((c["cost_usd"] for c in cases), default=0.0),
        "latency_ms_p50": lat[len(lat) // 2] if lat else None,
        "latency_ms_max": lat[-1] if lat else None,
    }


def total(points: list[int | None]) -> int:
    return sum(p or 0 for p in points)


def charge(budget: Budget, cost: float) -> None:
    """the marker's calls carry no request correlation id, so the cost is read from the teacher's ledger."""
    budget.after_cost(KIND, cost)


def rescore(cases: list[dict], teacher: Session, room: str) -> list[dict]:
    done = []
    for case in cases:
        done.append(
            {
                **case,
                "status": 200,
                "evidence": evidence_of(teacher, room, case),
                "cost_usd": 0.0,
                "latency_ms": 0,
            }
        )
        assess(done[-1])
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="")
    ap.add_argument("--case", action="append", help="run only this case (repeatable)")
    ap.add_argument(
        "--max-usd", type=float, default=DEFAULT_CAP, help=f"cost cap for the run (default {DEFAULT_CAP})"
    )
    ap.add_argument(
        "--rescore", action="store_true", help="score the suggestions already made; no submissions"
    )
    args = ap.parse_args()

    spec, state = fixture(), load_state()
    if "assessments" not in state.get(SECTION, {}):
        sys.exit("Run scripts/eval/load_marking.py first.")
    try:
        cases = select_cases(load_cases(spec, state), args.case)
    except ValueError as exc:
        ap.error(str(exc))
    room = state[SECTION]["classroom"]
    teacher = Session("teacher", state)
    out = RUNS / f"marking-{datetime.now():%Y%m%d-%H%M%S-%f}{'-' + args.label if args.label else ''}.json"
    if args.rescore:
        done = rescore(cases, teacher, room)
        checkpoint(
            out,
            {"status": "rescored", "planned_cases": len(cases), "summary": summarize(done), "cases": done},
        )
        print(f"{json.dumps(summarize(done), indent=2)}\nwrote {out}")
        return
    if again := [c["id"] for c in cases if submitted(Session(c["user"], state), c)]:
        sys.exit(
            f"Already submitted: {', '.join(again)}. Use --rescore to score them again, or run "
            "delete_marking.py and load_marking.py for a fresh run."
        )
    teacher_id = teacher.claims()["sub"]
    budget = Budget(args.max_usd, teacher_id)
    budget.preflight()
    done: list[dict] = []
    started = time.monotonic()

    def save(status: str, **extra: Any) -> None:
        checkpoint(
            out,
            {
                "status": status,
                "planned_cases": len(cases),
                "cap_usd": args.max_usd,
                "summary": summarize(done),
                "cases": done,
                **extra,
            },
        )

    save("running")
    print(f"{len(cases)} cases; checkpoint: {out}", flush=True)
    try:
        for case in cases:
            budget.before(KIND)
            completed = run_case(case, teacher, room, state, teacher_id)
            charge(budget, completed["cost_usd"])
            assess(completed)
            done.append(completed)
            save("running")
            mark = "FAIL" if completed["failures"] else ("ok" if completed["agrees"] else "differs")
            print(
                f"[{len(done)}/{len(cases)}] {mark}: {case['id']} {completed['first']} vs {case['expect']} "
                f"{completed['outcome']}/{completed['review']} (${completed['cost_usd']:.4f})",
                flush=True,
            )
    except CapReachedError as capped:
        save("capped", not_run=[c["id"] for c in cases[len(done) :]], reason=str(capped))
        print(f"Stopped: {capped} Partial results in {out}")
        sys.exit(2)
    except BaseException:
        save("interrupted")
        print(f"Partial results saved to {out}", flush=True)
        raise
    summary = summarize(done) | {"seconds": round(time.monotonic() - started)}
    for c in done:
        for f in c["failures"]:
            print(f"  FAIL {c['id']}: {f}")
    print(f"\n{json.dumps(summary, indent=2)}")
    checkpoint(
        out,
        {
            "status": "complete",
            "planned_cases": len(cases),
            "cap_usd": args.max_usd,
            "summary": summary,
            "cases": done,
        },
    )
    print(f"\nwrote {out}")
    sys.exit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()
