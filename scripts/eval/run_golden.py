"""score content's search on the golden queries, through kong, as real local users."""

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import yaml
from common import GOLDEN, RUNS, USERS, Session, corpus, load_state
from reporting import checkpoint, select_cases

TOP_K = 10
MARGIN = 0.02


def search(
    session: Session, classrooms: dict, q: dict, documents: dict, rerank: bool
) -> tuple[list[dict], bool]:
    """results for one query, best first, and whether any search fell back from the rerank."""
    filters = q.get("filters") or {}
    rooms = filters.get("class_ids") or list(USERS[q["as"]][1])
    titles = filters.get("titles")
    results: list[dict] = []
    fallback = False
    for c in rooms:
        body: dict = {"classroom_id": classrooms[c], "query": q["query"], "top_k": TOP_K, "rerank": rerank}
        if titles:
            body["document_ids"] = [documents[t]["id"] for t in titles if documents[t]["class"] == c]
            if not body["document_ids"]:
                continue
        if filters.get("doc_types"):
            body["doc_types"] = filters["doc_types"]
        res = session.request("POST", "/content/search", json=body)
        if res.status_code == 403:
            continue  # no access to that classroom: the filter can't widen access
        if res.is_error:
            sys.exit(f"{q['id']}: search -> {res.status_code}: {res.text[:300]}")
        data = res.json()
        fallback = fallback or data["rerank_fallback"]
        results += data["results"]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:TOP_K], fallback


def matches(result: dict, want: dict) -> bool:
    if result["title"] != want["title"]:
        return False
    if "page" in want and not (result["page_start"] or 0) <= want["page"] <= (result["page_end"] or 0):
        return False
    if "section" in want and want["section"].lower() not in (result["section_path"] or "").lower():
        return False
    return "kind" not in want or result.get("kind", "text") == want["kind"]


def alternatives(part: dict | list[dict]) -> list[dict]:
    """an `expect_all` part is one location, or a list of locations any of which answers that part."""
    return part if isinstance(part, list) else [part]


def recall(results: list[dict], parts: list) -> float:
    """share of `expect_all` parts answered by some result (one chunk may answer several parts)."""
    return sum(any(matches(r, w) for r in results for w in alternatives(p)) for p in parts) / len(parts)


def leaks_of(q: dict, results: list[dict], classes: dict[str, str]) -> list[str]:
    forbidden = set(q.get("expect_none_from") or [])
    out = {r["title"] for r in results if r["title"] in forbidden}
    if q["as"] != "t1":
        mine = set(USERS[q["as"]][1])
        out |= {
            r["title"]
            for r in results
            if r["doc_type"] != "teaching_material" or classes.get(r["title"]) not in mine
        }
    return sorted(out)


def score(q: dict, results: list[dict], classes: dict[str, str]) -> dict:
    wants = q.get("expect") or [w for p in q.get("expect_all") or [] for w in alternatives(p)]
    correct = [any(matches(r, w) for w in wants) for r in results]
    rank = next((i for i, ok in enumerate(correct, 1) if ok), None)
    hit = results[rank - 1] if rank else None
    scores = [r.get("score") for r in results]
    margin = None
    if rank and hit.get("score") is not None:
        # rank 1: lead over the best wrong result below; otherwise: gap to the best wrong result above (< 0)
        wrong = [
            s
            for i, s in enumerate(scores)
            if not correct[i] and s is not None and (i < rank - 1 or rank == 1)
        ]
        if wrong:
            margin = round(hit["score"] - max(wrong), 4)
    return {
        "id": q["id"],
        "as": q["as"],
        "query": q["query"],
        "group": q.get("group", "default"),
        "style": q.get("style", "clean"),
        "split": q.get("split", "tune"),
        "scored": bool(wants),
        "unanswerable": bool(q.get("unanswerable")),
        "kind_checked": any("kind" in w for w in wants),
        "rank": rank,
        "leaks": leaks_of(q, results, classes),
        "recall@5": round(recall(results[:5], q["expect_all"]), 3) if q.get("expect_all") else None,
        "recall@10": round(recall(results[:10], q["expect_all"]), 3) if q.get("expect_all") else None,
        "top_score": scores[0] if scores else None,
        "margin": margin,
        "scores": [round(s, 4) if s is not None else None for s in scores],
        "span": hit["page_end"] - hit["page_start"] + 1 if hit and hit["page_start"] else None,
        "top3": [
            f"{r['title']} p{r['page_start']}-{r['page_end']} [{r.get('kind', 'text')}] {r['section_path'] or ''}".strip()
            for r in results[:3]
        ],
    }


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["scored"]]
    n = len(scored) or 1
    hit = lambda k: sum(1 for r in scored if r["rank"] and r["rank"] <= k) / n  # noqa: E731
    out = {
        "queries": len(rows),
        "scored": len(scored),
        "unanswerable": sum(1 for r in rows if r["unanswerable"]),
        "hit@1": round(hit(1), 3),
        "hit@3": round(hit(3), 3),
        "hit@5": round(hit(5), 3),
        "mrr": round(sum(1 / r["rank"] for r in scored if r["rank"]) / n, 3),
        "leaks": sum(len(r["leaks"]) for r in rows),
        "mean_span": round(sum(spans) / len(spans), 2)
        if (spans := [r["span"] for r in scored if r["span"]])
        else None,
    }
    if multi := [r for r in rows if r.get("recall@5") is not None]:
        out["recall@5"] = round(sum(r["recall@5"] for r in multi) / len(multi), 3)
        out["recall@10"] = round(sum(r["recall@10"] for r in multi) / len(multi), 3)
    return out


def paired_bootstrap(before: list[dict], after: list[dict], n: int = 2000, seed: int = 0) -> dict | None:
    """95% intervals of the per-query change in reciprocal rank and hit@1, over queries both runs scored."""
    old = {r["id"]: r for r in before if r["scored"]}
    pairs = [(old[r["id"]], r) for r in after if r["scored"] and r["id"] in old]
    if not pairs:
        return None
    rr = lambda r: 1 / r["rank"] if r["rank"] else 0.0  # noqa: E731
    h1 = lambda r: 1.0 if r["rank"] == 1 else 0.0  # noqa: E731
    rng = random.Random(seed)
    out: dict = {"paired": len(pairs)}
    for name, f in (("mrr", rr), ("hit@1", h1)):
        diffs = [f(b) - f(a) for a, b in pairs]
        means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n))
        lo, hi = round(means[int(0.025 * n)], 4), round(means[int(0.975 * n) - 1], 4)
        out[name] = {
            "delta": round(sum(diffs) / len(diffs), 4),
            "ci95": [lo, hi],
            "changed": lo > 0 or hi < 0,
        }
    return out


def floor_table(rows: list[dict], step: float = 0.05) -> dict | None:
    """refuse when the top score is under a floor: how well does each floor separate answerable from unanswerable?"""
    good = [r["top_score"] for r in rows if r["scored"] and r["rank"] == 1 and r["top_score"] is not None]
    bad = [r["top_score"] if r["top_score"] is not None else float("-inf") for r in rows if r["unanswerable"]]
    if not good or not bad:
        return None
    grid = {round(i * step, 4) for i in range(int(1 / step) + 1)} | {
        s for s in good + bad if s != float("-inf")
    }
    table = []
    for t in sorted(grid):
        refused_bad, refused_good = sum(1 for s in bad if s < t), sum(1 for s in good if s < t)
        precision = refused_bad / (refused_bad + refused_good) if refused_bad + refused_good else 1.0
        rec = refused_bad / len(bad)
        f1 = 2 * precision * rec / (precision + rec) if precision + rec else 0.0
        table.append(
            {
                "floor": round(t, 4),
                "refuse_precision": round(precision, 3),
                "refuse_recall": round(rec, 3),
                "answered_kept": round(1 - refused_good / len(good), 3),
                "f1": round(f1, 3),
            }
        )
    best = max(table, key=lambda r: (r["f1"], r["answered_kept"]))
    wrong_top = sorted(
        (r["top_score"] for r in rows if r["scored"] and r["rank"] != 1 and r["top_score"] is not None),
        reverse=True,
    )
    return {
        "answerable": len(good),
        "unanswerable": len(bad),
        "best": best,
        "table": table,
        "wrong_top1_scores": [round(s, 4) for s in wrong_top],
    }


def kind_regressions(rows: list[dict], before: dict[str, dict]) -> list[str]:
    out = []
    for r in rows:
        if not r["kind_checked"]:
            continue
        if before.get(r["id"]):
            was = before[r["id"]]["rank"]
            if was and (not r["rank"] or r["rank"] > was):
                out.append(r["id"])
        elif not r["rank"]:
            out.append(r["id"])
    return out


def at(floor: dict, value: float) -> str:
    good = floor["answerable"]
    row = min(floor["table"], key=lambda r: abs(r["floor"] - value))
    return f"at {row['floor']}: refuses {row['refuse_recall']:.3f} of unanswerable, keeps {row['answered_kept']:.3f} of {good} correct answers"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="", help="suffix for the summary file, e.g. baseline")
    ap.add_argument("--golden", type=Path, default=GOLDEN, help="query file (default: golden.yaml)")
    ap.add_argument("--compare", type=Path, help="an earlier summary JSON to compare against")
    ap.add_argument("--rerank", action="store_true", help="search with rerank: true (fails on any fallback)")
    ap.add_argument("--holdout", action="store_true", help="also run the holdout split")
    ap.add_argument(
        "--case",
        action="append",
        help="run only this query ID (repeatable; holdout still requires --holdout)",
    )
    args = ap.parse_args()

    state = load_state()
    if "documents" not in state:
        sys.exit("Run scripts/eval/load_corpus.py first.")
    queries = yaml.safe_load(args.golden.read_text(encoding="utf-8")) or []
    if len({q["id"] for q in queries}) != len(queries):
        sys.exit("golden.yaml: duplicate ids")
    if not args.holdout:
        queries = [q for q in queries if q.get("split", "tune") != "holdout"]
    skipped = [q["id"] for q in queries if "topic_ids" in (q.get("filters") or {})]
    queries = [q for q in queries if q["id"] not in skipped]
    try:
        queries = select_cases(queries, args.case)
    except ValueError as exc:
        ap.error(str(exc))
    before_rows = json.loads(args.compare.read_text(encoding="utf-8"))["rows"] if args.compare else []
    before = {r["id"]: r for r in before_rows}

    classes = {m["title"]: m["class_ids"][0] for m in corpus().values()}
    classrooms, documents = state["classrooms"], state["documents"]
    sessions = {u: Session(USERS[u][0], state) for u in {q["as"] for q in queries}}
    rows = []
    out = RUNS / (
        datetime.now().strftime("%Y%m%d-%H%M%S-%f") + (f"-{args.label}" if args.label else "") + ".json"
    )

    def save_partial() -> None:
        checkpoint(out, {"status": "incomplete", "planned_queries": len(queries), "rows": rows})

    save_partial()
    for q in queries:
        results, fallback = search(sessions[q["as"]], classrooms, q, documents, args.rerank)
        if args.rerank and fallback:
            sys.exit(f"{q['id']}: the reranker failed (rerank_fallback), so this run would score RRF order")
        row = score(q, results, classes)
        rows.append(row)
        save_partial()
        rank = "n/a" if row["unanswerable"] else ("-" if not row["scored"] else (row["rank"] or "miss"))
        delta = f" (was {before[q['id']]['rank'] or 'miss'})" if q["id"] in before and row["scored"] else ""
        extra = f" top={row['top_score']:.3f}" if row["unanswerable"] and row["top_score"] is not None else ""
        if row["recall@10"] is not None:
            extra += f" recall@10={row['recall@10']}"
        leak = f"  LEAK: {', '.join(row['leaks'])}" if row["leaks"] else ""
        print(f"{q['as']}  {rank!s:>4}{delta:12} {q['id']}{extra}{leak}", flush=True)
        if row["scored"] and (not row["rank"] or row["rank"] > 3):
            for t in row["top3"]:
                print(f"            got: {t}")

    summary: dict = summarize(rows) | {"rerank": args.rerank, "holdout": args.holdout, "skipped": skipped}
    for field in ("group", "style", "split"):
        values = sorted({r[field] for r in rows})
        if len(values) > 1:
            summary[f"{field}s"] = {v: summarize([r for r in rows if r[field] == v]) for v in values}
            for v, sub in summary[f"{field}s"].items():
                print(f"\n[{field}: {v}] " + "  ".join(f"{k}={x}" for k, x in sub.items()))
    print("\n[all] " + "  ".join(f"{k}={v}" for k, v in summary.items() if not isinstance(v, dict | list)))
    if skipped:
        print(f"[skipped: topic_ids filter has no Content equivalent] {', '.join(skipped)}")
    if args.rerank and (floor := floor_table(rows)):
        summary["floor"] = floor
        b = floor["best"]
        print(
            f"\n[floor] {floor['answerable']} answerable (correct top-1) vs {floor['unanswerable']} unanswerable; best floor {b['floor']}: "
            f"refuse precision {b['refuse_precision']}, recall {b['refuse_recall']}, answerable kept {b['answered_kept']}"
        )
        print(f"  the chatbot's RELEVANCE_FLOOR 0.61 {at(floor, 0.61)}")
        if floor["wrong_top1_scores"]:
            print(f"  wrong top-1 scores (answerable): {floor['wrong_top1_scores']}")
    if before_rows and (boot := paired_bootstrap(before_rows, rows)):
        summary["compare"] = boot | {"against": args.compare.name}
        for name in ("mrr", "hit@1"):
            b = boot[name]
            print(
                f"[compare] {name} {b['delta']:+.4f}  95% CI [{b['ci95'][0]:+.4f}, {b['ci95'][1]:+.4f}]  "
                f"{'CHANGED' if b['changed'] else 'within noise'} ({boot['paired']} paired)"
            )
        if args.rerank:
            notable = [
                f"{r['id']} {before[r['id']]['rank'] or 'miss'}->{r['rank'] or 'miss'}"
                for r in rows
                if r["id"] in before
                and r["scored"]
                and r["rank"] != before[r["id"]]["rank"]
                and max(abs(r["margin"] or 0), abs(before[r["id"]].get("margin") or 0)) > MARGIN
            ]
            if notable:
                print(f"[compare] rank changes with margin > {MARGIN}: {', '.join(notable)}")
    regressed = kind_regressions(rows, before)
    if regressed:
        print(f"KIND REGRESSION: {', '.join(regressed)}")
    checkpoint(out, {"status": "complete", "planned_queries": len(queries), "summary": summary, "rows": rows})
    print(f"saved {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")
    if summary["leaks"] or regressed:
        sys.exit(1)


if __name__ == "__main__":
    main()
