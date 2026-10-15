"""Load the golden set's test corpus into the local stack and wait until Content has indexed it.

    python scripts/eval/load_corpus.py [--reindex]

- Creates two classrooms as `teacher` ("Golden eval C1" and "Golden eval C2") and enrolls `student` in C1 and a
  second local test student, eval-student2, in C2. eval-student2 is created in the local Keycloak realm the first
  time, with a generated password kept in .local/eval-corpus.json (gitignored).
- Uploads each PDF in corpus.yaml from EVAL_PDF_DIR into its class's classroom, as its doc_type.
- Safe to re-run: documents already in the classroom are kept, failed ones are queued again. --reindex queues
  every document again, after a change to ingest.

Indexing calls ai-core, so it costs OpenAI embeddings, plus vision calls for pages ai-core hasn't cached.
"""

import argparse
import secrets
import sys
import time

from common import CLASSES, PDF_DIR, STUDENT2, Session, corpus, load_state, save_state
from local_stack import admin_client, check, ensure_person


def ensure_student2(state: dict) -> None:
    """Create eval-student2 (realm role student, org local-school) in the local realm if it doesn't exist. Its
    password is generated once and kept in the eval's state; an existing account without one gets a new one."""
    known = "student2_password" in state
    if not known:
        state["student2_password"] = secrets.token_hex(20)
    with admin_client() as admin:
        ensure_person(
            admin, STUDENT2, "Eval", "Student", state["student2_password"], reset_password=not known
        )
    save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reindex", action="store_true", help="index every document again")
    args = ap.parse_args()
    manifest = corpus()
    missing = [name for name in manifest if not (PDF_DIR / name).exists()]
    if missing:
        sys.exit(f"Missing PDFs in {PDF_DIR} (set EVAL_PDF_DIR): {', '.join(missing)}")
    state = load_state()
    ensure_student2(state)
    teacher, student, student2 = (
        Session("teacher", state),
        Session("student", state),
        Session(STUDENT2, state),
    )
    profiles = {
        name: check(s.request("POST", "/identity/me", json={})).json()
        for name, s in (("teacher", teacher), ("student", student), ("student2", student2))
    }
    if profiles["student2"]["org_id"] != "local-school" or profiles["student2"]["role"] != "student":
        sys.exit(f"{STUDENT2} has no local-school student identity: {profiles['student2']}")

    mine = {c["id"] for c in check(teacher.request("GET", "/identity/classrooms")).json()}
    classrooms = state.get("classrooms", {})
    for c in CLASSES:
        if classrooms.get(c) not in mine:
            classrooms[c] = check(
                teacher.request("POST", "/identity/classrooms", json={"name": f"Golden eval {c}"})
            ).json()["id"]
    state["classrooms"] = classrooms
    save_state(state)
    for c, who in (("C1", "student"), ("C2", "student2")):
        check(
            teacher.request(
                "POST",
                f"/identity/classrooms/{classrooms[c]}/enrollments",
                json={"student_id": profiles[who]["id"]},
            )
        )

    documents: dict[str, dict] = {}
    for c in CLASSES:
        for d in check(teacher.request("GET", f"/content/documents?classroom_id={classrooms[c]}")).json():
            documents[d["title"]] = d
    for name, meta in manifest.items():
        title, room = meta["title"], classrooms[meta["class_ids"][0]]
        existing = documents.get(title)
        if existing and (
            existing["status"] == "failed" or (args.reindex and existing["status"] != "indexing")
        ):
            check(teacher.request("POST", f"/content/documents/{existing['id']}/reindex", json={}))
        elif not existing:
            data = (PDF_DIR / name).read_bytes()
            documents[title] = check(
                teacher.request(
                    "POST",
                    "/content/documents",
                    params={"classroom_id": room, "title": title, "doc_type": meta["doc_type"]},
                    content=data,
                    headers={"Content-Type": "application/pdf"},
                )
            ).json()
            print(f"uploaded {title}")

    started = time.monotonic()
    while True:
        listed = {
            d["title"]: d
            for c in CLASSES
            for d in check(teacher.request("GET", f"/content/documents?classroom_id={classrooms[c]}")).json()
        }
        pending = [
            m["title"] for m in manifest.values() if listed[m["title"]]["status"] not in ("indexed", "failed")
        ]
        if not pending:
            break
        if time.monotonic() - started > 3600:
            sys.exit(f"Still indexing after an hour: {', '.join(pending)}")
        print(
            f"  indexing {len(pending)}: {', '.join(pending[:4])}{' …' if len(pending) > 4 else ''}",
            flush=True,
        )
        time.sleep(15)
    state["documents"] = {
        m["title"]: {"id": listed[m["title"]]["id"], "class": m["class_ids"][0]} for m in manifest.values()
    }
    save_state(state)
    for m in manifest.values():
        d = listed[m["title"]]
        print(
            f"  {d['status']:8} pages={d.get('page_count') or '-':>3} chunks={d.get('chunk_count') or '-':>4}  {m['title']} [{m['doc_type']}, {m['class_ids'][0]}]"
            + (f"  {d.get('failure_reason')}" if d["status"] == "failed" else "")
        )
    if any(listed[m["title"]]["status"] == "failed" for m in manifest.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
