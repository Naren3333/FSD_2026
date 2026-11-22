"""run the question generation eval (adr 0006, 0015) within a cost cap: each case through kong as the teacher with the review on and off, with --practice as the classroom's enrolled student through the practice route (adr 0022), and with --probe planted drafts sent straight to the deployed reviewer to measure what it catches."""

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from budget import DEFAULT_CAP, Budget, CapReachedError, correlation, ledger_calls
from common import ROOT, RUNS, STUDENT2, Session, load_state, save_state, service_modules
from local_stack import check, psql_json
from reporting import checkpoint, select_cases

# the agent's own checks, to hold its output to them; its prompts for the reviewer's reasons.
checks, contracts, qg_prompts = service_modules("question-generation-agent", "checks", "contracts", "prompts")

CASES = Path(__file__).with_name("generation_cases.yaml")
PROBE = Path(__file__).with_name("review_probe.yaml")
INSIDE = Path(__file__).with_name("probe_inside.py")
PATH = "/question-generation/questions"
PRACTICE_PATH = "/question-generation/practice"
PRACTICE_TYPES = ("multiple_choice", "short_answer")  # what a student may practise
# the corpus classrooms' enrolled students (load_corpus.py).
STUDENTS = {"C1": "student", "C2": STUDENT2}
TIMEOUT = 310
MAX_CALLS = 6  # three writes and three reviews (adr 0015)
MAX_LATENCY_MS = 300_000  # kong's read timeout


def load_cases() -> tuple[str, list[dict]]:
    spec = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    return spec["prefix"], spec["cases"]


def runs_of(cases: list[dict], ab: bool) -> list[dict]:
    """each case with the review on, then off unless the case or the run says not to."""
    out = []
    for c in cases:
        out.append({**c, "run": f"{c['id']}/review", "review": True})
        if ab and c.get("ab", True):
            out.append({**c, "run": f"{c['id']}/no-review", "review": False})
    return out


def practice_runs(cases: list[dict]) -> list[dict]:
    """each case once as practice, with the types a student may not practise left out; a case left with none is skipped."""
    out = []
    for c in cases:
        counts = {t: n for t, n in c["counts"].items() if t in PRACTICE_TYPES}
        if counts:
            out.append(
                {**c, "run": f"{c['id']}/practice", "review": True, "practice": True, "counts": counts}
            )
    return out


def room_of(run: dict, state: dict) -> str:
    """the corpus classroom (C1, C2) the case's documents share."""
    rooms = {state["documents"][t]["class"] for t in run["documents"]}
    if len(rooms) != 1:
        raise SystemExit(f"{run['id']}: documents {run['documents']} are in different classrooms")
    return rooms.pop()


def ensure_topics(teacher: Session, prefix: str, cases: list[dict], state: dict) -> dict[str, str]:
    ids: dict[str, str] = state.setdefault("generation", {}).setdefault("topics", {})
    for name in dict.fromkeys(c["topic"] for c in cases):
        if name in ids and teacher.request("GET", f"/curriculum/topics/{ids[name]}").status_code == 200:
            continue
        ids[name] = check(teacher.request("POST", "/curriculum/topics", json={"name": prefix + name})).json()[
            "id"
        ]
        save_state(state)
    return ids


def chunks_of(document_ids: list[str]) -> dict[str, str]:
    """chunk id → document id, for the documents the cases use."""
    listed = ",".join(f"'{d}'" for d in document_ids)
    rows = psql_json(
        database="content", sql=f"SELECT id, document_id FROM document_chunks WHERE document_id IN ({listed})"
    )
    return {r["id"]: r["document_id"] for r in rows}


def run_case(run: dict, caller: Session, state: dict, topics: dict[str, str]) -> dict:
    """one case through kong: as the teacher, or for a practice run as the classroom's enrolled student (`caller`), who cannot skip the review."""
    docs = [state["documents"][t] for t in run["documents"]]
    body = {
        "classroom_id": state["classrooms"][room_of(run, state)],
        "document_ids": [d["id"] for d in docs],
        "topic_id": topics[run["topic"]],
        "counts": run["counts"],
        "difficulty": run["difficulty"],
        "focus": run.get("focus", ""),
        "existing_prompts": run.get("existing", []),
    }
    if not run.get("practice"):
        body["review"] = run["review"]
    sent = correlation()
    started = time.monotonic()
    path = PRACTICE_PATH if run.get("practice") else PATH
    res = caller.request("POST", path, json=body, headers={"X-Correlation-ID": sent}, timeout=TIMEOUT)
    try:
        reply = res.json()
    except ValueError:
        reply = {"raw": res.text[:500]}
    return {
        **run,
        "document_ids": body["document_ids"],
        "topic_id": body["topic_id"],
        "correlation": sent,
        "status": res.status_code,
        "body": reply,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def drafts_of(run: dict) -> list[dict]:
    """the run's questions in the teacher's draft shape; practice returns only questions its reviewer passed (adr 0022), so each is marked passed."""
    questions = run["body"].get("questions", [])
    if not run.get("practice"):
        return questions
    return [
        {
            "question": {
                "prompt": q.get("prompt", ""),
                "type": q.get("type"),
                "topic_id": q.get("topic_id") or run["topic_id"],
                "points": 1,
                "options": q.get("options", []),
                "answer": q.get("answer", ""),
                "accepted_answers": q.get("accepted_answers", []),
                "sources": q.get("sources", []),
            },
            "evidence": q.get("evidence", ""),
            "explanation": q.get("explanation", ""),
            "review": {"status": "passed", "note": ""},
        }
        for q in questions
    ]


def practice_failures(body: dict) -> list[str]:
    """what a practice question must carry for the student to check an answer and learn from it."""
    failures = []
    for n, q in enumerate(body.get("questions", []), 1):
        if q.get("type") not in PRACTICE_TYPES:
            failures.append(f"question {n} is a {q.get('type')}, which practice never asks for")
        if q.get("type") == "short_answer" and not q.get("accepted_answers"):
            failures.append(f"question {n} has no accepted answers")
        if not (q.get("explanation") or "").strip():
            failures.append(f"question {n} has no explanation")
        if not q.get("sources"):
            failures.append(f"question {n} has no sources")
    return failures


def assess(run: dict, chunks: dict[str, str]) -> None:
    """adds `failures`, `flags` and a `digest` to a completed run."""
    expect, body = run.get("expect") or {}, run["body"]
    failures: list[str] = []
    flags: list[str] = []
    want = expect.get("status", 200)
    if run["status"] != want:
        failures.append(f"HTTP {run['status']}, expected {want}: {json.dumps(body)[:300]}")
    elif want != 200:
        if (code := (body.get("error") or {}).get("code")) != expect.get("code"):
            failures.append(f"error {code}, expected {expect.get('code')}")
        if run["generate_calls"]:
            failures.append(f"{run['generate_calls']} generate calls for a refusal; it should make none")
    else:
        drafts = drafts_of(run)
        if run.get("practice"):
            failures += practice_failures(body)
        chosen = set(run["document_ids"])
        for n, g in enumerate(drafts, 1):
            q = g["question"]
            for s in q["sources"]:
                if s.get("chunk_id") not in chunks or chunks[s["chunk_id"]] not in chosen:
                    failures.append(f"draft {n} cites chunk {s.get('chunk_id')} outside the chosen documents")
            if q["type"] == "multiple_choice" and q["answer"] not in q["options"]:
                failures.append(f"draft {n}: key {q['answer']!r} is not an option")
            if checks.NEEDS_MATERIALS_PATTERN.search(checks.normalize(q["prompt"])):
                failures.append(f"draft {n} leans on the materials: {q['prompt'][:80]!r}")
            for other in drafts[: n - 1]:
                if checks.similar(q["prompt"], other["question"]["prompt"]):
                    failures.append(f"draft {n} repeats another draft: {q['prompt'][:80]!r}")
            for existing in run.get("existing", []):
                if checks.similar(q["prompt"], existing):
                    failures.append(f"draft {n} repeats existing question {existing!r}")
            if checks.long_key(contracts.DraftQuestion.model_validate(q)):
                flags.append(f"draft {n} has a long key")
        if body.get("model_calls", 0) > MAX_CALLS:
            failures.append(f"{body['model_calls']} model calls, over {MAX_CALLS}")
        if run["latency_ms"] > MAX_LATENCY_MS:
            failures.append(f"took {run['latency_ms']} ms, past kong's timeout")
        returned = Counter(g["question"]["type"] for g in drafts)
        if short := {t: n - returned[t] for t, n in run["counts"].items() if returned[t] < n}:
            flags.append(f"short of {short}")
        statuses = Counter(g["review"]["status"] for g in drafts)
        if run["review"] and (statuses["flagged"] or statuses["not_reviewed"]):
            flags.append(f"{statuses['flagged']} flagged, {statuses['not_reviewed']} not reviewed")
    run["failures"], run["flags"] = failures, flags
    run["digest"] = digest(run)


def digest(run: dict) -> dict:
    body = run["body"]
    drafts = drafts_of(run) if run["status"] == 200 else []
    rejected: Counter[str] = Counter()
    for r in body.get("rounds", []):
        rejected.update(r.get("rejected", {}))
    if run.get("practice"):
        rejected.update(body.get("dropped", {}))  # practice reports no rounds, only what it dropped
    return {
        "returned": body.get("returned"),
        "requested": body.get("requested"),
        "statuses": dict(Counter(g["review"]["status"] for g in drafts)),
        "drafts": [
            f"[{g['review']['status']}] {g['question']['type']}: {g['question']['prompt']}"
            + (f" — {g['review']['note']}" if g["review"].get("note") else "")
            for g in drafts
        ],
        "rounds": len(body.get("rounds", [])),
        "rejected": dict(rejected),
        "model_calls": body.get("model_calls"),
        "cost_usd": run["cost_usd"],
        "latency_ms": run["latency_ms"],
    }


def mode_of(run: dict) -> str:
    if run.get("practice"):
        return "practice"
    return "review" if run["review"] else "no-review"


def summarize(runs: list[dict]) -> dict:
    modes: dict[str, dict] = {}
    for mode in ("review", "no-review", "practice"):
        rows = [r for r in runs if mode_of(r) == mode and r["status"] == 200]
        if not rows:
            continue
        statuses: Counter[str] = Counter()
        rejected: Counter[str] = Counter()
        for r in rows:
            statuses.update(r["digest"]["statuses"])
            rejected.update(r["digest"]["rejected"])
        lat = sorted(r["latency_ms"] for r in rows)
        modes[mode] = {
            "runs": len(rows),
            "returned_rate": round(
                sum(r["digest"]["returned"] or 0 for r in rows)
                / sum(r["digest"]["requested"] or 0 for r in rows),
                3,
            ),
            "statuses": dict(statuses),
            "rejected": dict(rejected),
            "rounds": sum(r["digest"]["rounds"] for r in rows),
            "model_calls": sum(r["digest"]["model_calls"] or 0 for r in rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "latency_ms_p50": lat[len(lat) // 2],
            "latency_ms_max": lat[-1],
        }
    summary: dict[str, Any] = {
        "runs": len(runs),
        "failed": sum(bool(r["failures"]) for r in runs),
        "flagged": sum(bool(r["flags"]) for r in runs),
        "failures": sum(len(r["failures"]) for r in runs),
        "cost_usd": round(sum(r["cost_usd"] for r in runs), 6),
        "modes": modes,
    }
    if "review" in modes and "no-review" in modes:
        on, off = modes["review"], modes["no-review"]
        summary["review_minus_no_review"] = {
            k: round(on[k] - off[k], 6)
            for k in ("returned_rate", "model_calls", "cost_usd", "latency_ms_p50")
        }
    return summary


# ---- the reviewer probe


def probe_batches() -> tuple[dict, list[dict]]:
    spec = yaml.safe_load(PROBE.read_text(encoding="utf-8"))
    return spec, [{"id": k, **b} for k, b in spec["batches"].items()]


def review_batch(spec: dict, batch: dict, billed: dict, sent: str) -> list[dict]:
    """the deployed reviewer's verdicts on one batch, from inside the agent's container."""
    job = {
        "topic": spec["topic"],
        "difficulty": spec["difficulty"],
        "batch": batch,
        "billed_to": billed,
        "correlation": sent,
    }
    done = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "question-generation-agent",
            "python",
            "-c",
            INSIDE.read_text(encoding="utf-8"),
        ],
        check=False,
        cwd=ROOT,
        input=json.dumps(job),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=TIMEOUT,
    )
    if done.returncode:
        raise RuntimeError(f"probe batch {batch['id']} failed: {done.stderr.strip()[-600:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def judge(batch: dict, verdicts: list[dict], attempt: int) -> list[dict]:
    rows = []
    for d, v in zip(batch["drafts"], verdicts, strict=True):
        fair = {d["reason"], *d.get("also", [])} if d["expect"] == "fail" else {"NONE"}
        rows.append(
            {
                "id": d["id"],
                "batch": batch["id"],
                "attempt": attempt,
                "expect": d["expect"],
                "reason": d.get("reason", "NONE"),
                "passed": v["passed"],
                "got": v["reason"],
                "note": v["note"],
                "right": v["reason"] != "NOT_REVIEWED" and (v["passed"] == (d["expect"] == "pass")),
                "exact": v["reason"] in fair,
            }
        )
    return rows


def probe_summary(rows: list[dict]) -> dict:
    reviewed = [r for r in rows if r["got"] != "NOT_REVIEWED"]
    bad = [r for r in reviewed if r["expect"] == "fail"]
    good = [r for r in reviewed if r["expect"] == "pass"]
    by_reason: dict[str, str] = {}
    for reason in sorted({r["reason"] for r in bad}):
        mine = [r for r in bad if r["reason"] == reason]
        by_reason[reason] = (
            f"{sum(not r['passed'] for r in mine)}/{len(mine)} caught, {sum(r['exact'] for r in mine)} named"
        )
    return {
        "verdicts": len(rows),
        "not_reviewed": len(rows) - len(reviewed),
        "catch_rate": round(sum(not r["passed"] for r in bad) / len(bad), 3) if bad else None,
        "exact_reason_rate": round(sum(r["exact"] for r in bad) / len(bad), 3) if bad else None,
        "false_reject_rate": round(sum(not r["passed"] for r in good) / len(good), 3) if good else None,
        "by_reason": by_reason,
        "missed": sorted({r["id"] for r in bad if r["passed"]}),
        "wrongly_failed": sorted({f"{r['id']} ({r['got']})" for r in good if not r["passed"]}),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="")
    ap.add_argument("--case", action="append", help="run only this case ID (repeatable)")
    ap.add_argument(
        "--max-usd", type=float, default=DEFAULT_CAP, help=f"cost cap for the run (default {DEFAULT_CAP})"
    )
    ap.add_argument("--no-ab", action="store_true", help="run each case with the review on only")
    ap.add_argument(
        "--practice",
        action="store_true",
        help="run each case as student practice (adr 0022), as the classroom's enrolled student",
    )
    part = ap.add_mutually_exclusive_group()
    part.add_argument("--probe", action="store_true", help="also run the reviewer probe")
    part.add_argument("--probe-only", action="store_true", help="run only the reviewer probe")
    ap.add_argument("--repeat", type=int, default=1, help="reviewer probe passes (default 1)")
    args = ap.parse_args()

    state = load_state()
    prefix, cases = load_cases()
    if not args.probe_only and "documents" not in state:
        sys.exit("Run scripts/eval/load_corpus.py first.")
    try:
        cases = [] if args.probe_only else select_cases(cases, args.case)
    except ValueError as exc:
        ap.error(str(exc))
    teacher = Session("teacher", state)
    claims = teacher.claims()
    budget = Budget(args.max_usd, claims["sub"])
    budget.preflight()
    runs = practice_runs(cases) if args.practice else runs_of(cases, not args.no_ab)
    students = {room: Session(user, state) for room, user in STUDENTS.items()} if args.practice else {}
    topics = ensure_topics(teacher, prefix, cases, state) if runs else {}
    chunks = (
        chunks_of(sorted({state["documents"][t]["id"] for c in cases for t in c["documents"]}))
        if runs
        else {}
    )
    spec, batches = probe_batches() if (args.probe or args.probe_only) else ({}, [])
    out = RUNS / f"generation-{datetime.now():%Y%m%d-%H%M%S-%f}{'-' + args.label if args.label else ''}.json"
    done: list[dict] = []
    verdicts: list[dict] = []
    probe_cost = 0.0

    def report(status: str, **extra: Any) -> dict:
        return {
            "status": status,
            "planned_runs": len(runs),
            "cap_usd": args.max_usd,
            "summary": summarize(done),
            "probe": probe_summary(verdicts) | {"cost_usd": round(probe_cost, 6)} if verdicts else None,
            "runs": done,
            "probe_verdicts": verdicts,
            **extra,
        }

    checkpoint(out, report("running"))
    print(f"{len(runs)} runs, {len(batches) * args.repeat} probe batches; checkpoint: {out}", flush=True)
    try:
        for run in runs:
            budget.before("generation")
            caller = students[room_of(run, state)] if run.get("practice") else teacher
            completed = run_case(run, caller, state, topics)
            completed["cost_usd"] = budget.after("generation", completed["correlation"])
            completed["generate_calls"] = ledger_calls(completed["correlation"], "generate")
            assess(completed, chunks)
            done.append(completed)
            checkpoint(out, report("running"))
            mark = "FAIL" if completed["failures"] else ("flag" if completed["flags"] else "ok")
            print(
                f"[{len(done)}/{len(runs)}] {mark}: {run['run']} (${completed['cost_usd']:.4f})", flush=True
            )
        billed = {"user_id": claims["sub"], "org_id": claims["org_id"]}
        for attempt in range(1, args.repeat + 1):
            for batch in batches:
                budget.before("probe")
                sent = correlation()
                got = review_batch(spec, batch, billed, sent)
                cost = budget.after("probe", sent)
                probe_cost += cost
                verdicts += judge(batch, got, attempt)
                checkpoint(out, report("running"))
                print(
                    f"probe {batch['id']} pass {attempt}: {sum(v['right'] for v in verdicts[-len(got) :])}/{len(got)} as expected (${cost:.4f})",
                    flush=True,
                )
    except CapReachedError as capped:
        checkpoint(out, report("capped", reason=str(capped)))
        print(f"Stopped: {capped} Partial results in {out}")
        sys.exit(2)
    except BaseException:
        checkpoint(out, report("interrupted"))
        print(f"Partial results saved to {out}", flush=True)
        raise
    final = report("complete")
    for r in done:
        mark = "FAIL" if r["failures"] else ("flag" if r["flags"] else "ok")
        print(
            f"  {mark:4} {r['run']:24} HTTP {r['status']}  {r['digest']['statuses']}  calls={r['digest']['model_calls']}  ${r['cost_usd']:.4f}"
        )
        for f in r["failures"] + r["flags"]:
            print(f"         - {f}")
    print(f"\n{json.dumps({'summary': final['summary'], 'probe': final['probe']}, indent=2)}")
    checkpoint(out, final)
    print(f"\nwrote {out}")
    sys.exit(1 if final["summary"]["failures"] else 0)


if __name__ == "__main__":
    main()
