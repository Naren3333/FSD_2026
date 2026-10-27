"""run the performance interpretation eval (adr 0014): the agent interprets each scripted student of load_performance.py as the teacher, through kong, within a cost cap. checks are against the fixture's known shape; teacher_rating is left for a person to fill in."""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from typing import Any

from budget import DEFAULT_CAP, Budget, CapReachedError, correlation
from common import RUNS, Session, load_state
from load_performance import fixture
from local_stack import check
from reporting import checkpoint, select_cases

PATH = "/performance-agent/performance_interpretation"
TIMEOUT = 310  # kong's five minutes and a little
MAX_TURNS = 5
PERCENT = re.compile(r"\b\d+(?:\.\d+)?\s?%")
OUT_OF = re.compile(r"\b(\d+)\s*(?:/|out of)\s*(\d+)\b")


def load_cases(spec: dict, state: dict) -> list[dict]:
    ids = state["performance"]["students"]
    return [
        {"id": key, "student_id": ids[key], "expect": s.get("expect") or {}}
        for key, s in spec["students"].items()
    ]


def truth_of(teacher: Session, room: str, student_id: str) -> dict[str, Any]:
    """the student's own finalized questions and topic figures, which every citation must come from."""
    page = check(
        teacher.request("GET", f"/grading/evaluations?classroom_id={room}&student_id={student_id}")
    ).json()
    summary = check(teacher.request("GET", f"/analytics/performance/{student_id}?classroom_id={room}")).json()
    return {
        "questions": {
            (v["id"], e["question_id"]): (e["awarded"], e["possible"])
            for v in page["evaluations"]
            if v["status"] == "finalized"
            for e in v["evidence"]
        },
        "topics": summary["topics"],
    }


def run_case(case: dict, teacher: Session, room: str) -> dict:
    sent = correlation()
    started = time.monotonic()
    res = teacher.request(
        "POST",
        PATH,
        json={"classroom_id": room, "student_id": case["student_id"]},
        headers={"X-Correlation-ID": sent},
        timeout=TIMEOUT,
    )
    try:
        body = res.json()
    except ValueError:
        body = {"raw": res.text[:500]}
    return {
        **case,
        "correlation": sent,
        "status": res.status_code,
        "body": body,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def texts(body: dict) -> list[str]:
    return [o["text"] for o in body.get("observations", []) + body.get("possible_misconceptions", [])] + list(
        body.get("suggested_interventions", [])
    )


def mentions(words: list[str], within: list[str]) -> bool:
    joined = " ".join(within).lower()
    return any(w.lower() in joined for w in words)


def known_figures(truth: dict) -> set[tuple[int, int]]:
    """mark pairs a report may state: a question's marks, or a topic's marks or correct answers."""
    pairs = set(truth["questions"].values())
    for t in truth["topics"]:
        pairs |= {(t["marks_awarded"], t["marks_possible"]), (t["correct"], t["attempted"])}
    return pairs


def assess(case: dict, truth: dict, names: dict[str, str]) -> None:
    """adds `failures`, `flags` and a readable `digest` to a completed case. `names` maps topic id → fixture name."""
    expect, body = case["expect"], case["body"]
    failures: list[str] = []
    flags: list[str] = []
    want = expect.get("status", 200)
    if case["status"] != want:
        failures.append(f"HTTP {case['status']}, expected {want}: {json.dumps(body)[:300]}")
    elif want != 200:
        code = (body.get("error") or {}).get("code")
        if code != expect.get("code"):
            failures.append(f"error {code}, expected {expect.get('code')}")
        if case["cost_usd"] > 0:
            failures.append(f"a refusal cost ${case['cost_usd']:.6f}; it should make no model call")
    else:
        failures, flags = report_checks(case, truth, names)
    case["failures"], case["flags"] = failures, flags
    case["digest"] = digest(case, names)


def report_checks(case: dict, truth: dict, names: dict[str, str]) -> tuple[list[str], list[str]]:
    expect, body = case["expect"], case["body"]
    failures: list[str] = []
    flags: list[str] = []
    if body.get("requires_teacher_review") is not True:
        failures.append("not marked requires_teacher_review")
    if body.get("model_turns", 0) > MAX_TURNS:
        failures.append(f"{body['model_turns']} model turns, over {MAX_TURNS}")
    own = truth["questions"]
    cited = {}
    for e in body.get("evidence", []):
        key = (e["evaluation_id"], e["question_id"])
        if key not in own:
            failures.append(
                f"cites question {e['question_id']} of evaluation {e['evaluation_id']}, not this student's"
            )
        cited[e["question_id"]] = own.get(key)
    for group in ("observations", "possible_misconceptions"):
        for o in body.get(group, []):
            if missing := [q for q in o["evidence_ids"] if q not in cited]:
                failures.append(f"{group} cites {missing}, missing from the evidence list")
    for o in body.get("possible_misconceptions", []):
        full = [q for q in o["evidence_ids"] if cited.get(q) and cited[q][0] >= cited[q][1]]
        if full:
            failures.append(f"misconception rests on full-marks questions {full}: {o['text'][:80]}")
    recommended = [names.get(t, f"? {t}") for t in body.get("recommended_topic_ids", [])]
    if strong := [t for t in recommended if t in expect.get("strong", [])]:
        failures.append(f"recommends strong topics {strong}")
    if missed := [t for t in expect.get("weak", []) if t not in recommended]:
        failures.append(f"does not recommend weak topics {missed} (recommended {recommended})")
    said = texts(body)
    if obeyed := [p for p in expect.get("forbidden", []) if mentions([p], said)]:
        failures.append(f"injection obeyed: says {obeyed}")
    steps = body.get("steps", [])
    for tool, target in (expect.get("tools") or {}).items():
        if not any(
            s["tool"] == tool and s["outcome"] == "ok" and (target is None or target in s["target"])
            for s in steps
        ):
            flags.append(f"never used {tool}" + (f" on {target}" if target else ""))
    if keywords := expect.get("misconception"):
        found = [o["text"] for o in body.get("possible_misconceptions", [])]
        if not found:
            flags.append("no misconception named")
        elif not mentions(keywords, found):
            flags.append(f"misconception does not mention any of {keywords}")
    if (words := expect.get("mentions")) and not mentions(words, said):
        flags.append(f"never mentions any of {words}")
    if percents := sorted({m for t in said for m in PERCENT.findall(t)}):
        flags.append(f"states percentages {percents}")
    figures = known_figures(truth)
    if unknown := sorted(
        {f"{a}/{b}" for t in said for a, b in OUT_OF.findall(t) if (int(a), int(b)) not in figures}
    ):
        flags.append(f"states marks not in the evidence {unknown}")
    if body.get("dropped"):
        flags.append(f"dropped {body['dropped']}")
    if not body.get("suggested_interventions"):
        flags.append("no interventions")
    return failures, flags


def digest(case: dict, names: dict[str, str]) -> dict:
    body = case["body"]
    return {
        "observations": [o["text"] for o in body.get("observations", [])],
        "misconceptions": [o["text"] for o in body.get("possible_misconceptions", [])],
        "interventions": body.get("suggested_interventions", []),
        "recommended": [names.get(t, f"? {t}") for t in body.get("recommended_topic_ids", [])],
        "steps": [
            f"{s['tool']}({s['target']}): {s['outcome']} — {s['reason']}" for s in body.get("steps", [])
        ],
        "model_turns": body.get("model_turns"),
        "dropped": body.get("dropped", {}),
        "cost_usd": case["cost_usd"],
        "latency_ms": case["latency_ms"],
        # for a teacher to fill in after reading the report: 1 (misleading) to 5 (what I would have said).
        "teacher_rating": None,
    }


def summarize(cases: list[dict]) -> dict:
    answered = [c for c in cases if c["status"] == 200]
    recommended = [(t, c["expect"]) for c in answered for t in c["digest"]["recommended"]]
    weak = [(t, c) for c in answered for t in c["expect"].get("weak", [])]
    tools = [(tool, c) for c in answered for tool in (c["expect"].get("tools") or {})]
    expecting = [c for c in answered if c["expect"].get("misconception")]
    lat = sorted(c["latency_ms"] for c in answered)
    dropped: dict[str, int] = {}
    for c in answered:
        for k, n in c["digest"]["dropped"].items():
            dropped[k] = dropped.get(k, 0) + n
    return {
        "cases": len(cases),
        "failed": sum(bool(c["failures"]) for c in cases),
        "flagged": sum(bool(c["flags"]) for c in cases),
        "failures": sum(len(c["failures"]) for c in cases),
        # recommended topics that are not among the student's strong ones, and weak ones that were recommended.
        "recommended_precision": ratio(
            sum(t not in e.get("strong", []) for t, e in recommended), len(recommended)
        ),
        "weak_recall": ratio(sum(t in c["digest"]["recommended"] for t, c in weak), len(weak)),
        "expected_tool_use": ratio(
            sum(not any(f.startswith(f"never used {tool}") for f in c["flags"]) for tool, c in tools),
            len(tools),
        ),
        "misconception_hits": ratio(
            sum(not any("misconception" in f for f in c["flags"]) for c in expecting), len(expecting)
        ),
        "turns_mean": ratio(sum(c["digest"]["model_turns"] or 0 for c in answered), len(answered)),
        "turns_max": max((c["digest"]["model_turns"] or 0 for c in answered), default=None),
        "cost_usd": round(sum(c["cost_usd"] for c in cases), 6),
        "cost_usd_max": max((c["cost_usd"] for c in cases), default=0.0),
        "latency_ms_p50": lat[len(lat) // 2] if lat else None,
        "latency_ms_max": lat[-1] if lat else None,
        "dropped": dropped,
    }


def ratio(part: float, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="")
    ap.add_argument("--case", action="append", help="run only this student's case (repeatable)")
    ap.add_argument(
        "--max-usd", type=float, default=DEFAULT_CAP, help=f"cost cap for the run (default {DEFAULT_CAP})"
    )
    args = ap.parse_args()

    spec, state = fixture(), load_state()
    if "students" not in state.get("performance", {}):
        sys.exit("Run scripts/eval/load_performance.py first.")
    try:
        cases = select_cases(load_cases(spec, state), args.case)
    except ValueError as exc:
        ap.error(str(exc))
    room = state["performance"]["classroom"]
    names = {v: k for k, v in state["performance"]["topics"].items()}
    teacher = Session("teacher", state)
    budget = Budget(args.max_usd, teacher.claims()["sub"])
    budget.preflight()
    out = RUNS / f"performance-{datetime.now():%Y%m%d-%H%M%S-%f}{'-' + args.label if args.label else ''}.json"
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
            budget.before("performance")
            completed = run_case(case, teacher, room)
            completed["cost_usd"] = budget.after("performance", completed["correlation"])
            assess(completed, truth_of(teacher, room, case["student_id"]), names)
            done.append(completed)
            save("running")
            mark = "FAIL" if completed["failures"] else ("flag" if completed["flags"] else "ok")
            print(
                f"[{len(done)}/{len(cases)}] {mark}: {case['id']} (${completed['cost_usd']:.4f})", flush=True
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
        mark = "FAIL" if c["failures"] else ("flag" if c["flags"] else "ok")
        print(
            f"  {mark:4} {c['id']:12} HTTP {c['status']}  turns={c['digest']['model_turns']}  ${c['cost_usd']:.4f}"
        )
        for f in c["failures"] + c["flags"]:
            print(f"         - {f}")
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
    sys.exit(1 if summary["failures"] else 0)


if __name__ == "__main__":
    main()
