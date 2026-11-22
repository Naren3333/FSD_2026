"""seed the marking eval idempotently (adr 0024): marking_cases.yaml's students, enrolled in the eval's classroom, and one published assessment with ai marking per question. no paid calls: nothing is submitted until run_marking.py."""

import argparse
import secrets
from pathlib import Path
from typing import Any

import yaml
from common import Session, load_state, save_state
from load_performance import ensure_classroom, ensure_topics
from local_stack import admin_client, check, ensure_person

FIXTURE = Path(__file__).with_name("marking_cases.yaml")
SECTION = "marking"


def fixture() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def username(n: int) -> str:
    return f"eval-mark-{n:02d}"


def students_needed(spec: dict) -> int:
    """one student per case of the busiest question: a student sits each assessment once."""
    counts: dict[str, int] = {}
    for c in spec["cases"]:
        counts[c["question"]] = counts.get(c["question"], 0) + 1
    return max(counts.values())


def assign(spec: dict) -> dict[str, str]:
    """case id → the username who answers it: the n-th case of a question goes to the n-th student."""
    seen: dict[str, int] = {}
    out = {}
    for c in spec["cases"]:
        seen[c["question"]] = seen.get(c["question"], 0) + 1
        out[c["id"]] = username(seen[c["question"]])
    return out


def question_input(q: dict, topic_id: str) -> dict:
    body: dict[str, Any] = {"prompt": q["prompt"], "type": q["type"], "topic_id": topic_id}
    if q.get("marking_scheme"):
        # the teacher's own scheme passages, pasted rather than cited from a document.
        body["marking_scheme"] = [{"text": t} for t in q["marking_scheme"]]
    if q["type"] == "short_answer":
        return body | {"accepted_answers": q["accepted"], "case_sensitive": bool(q.get("case_sensitive"))}
    rubric = {
        "criteria": [
            {"title": c["title"], "levels": [{"points": p, "descriptor": d} for p, d in c["levels"]]}
            for c in q["criteria"]
        ],
        "model_answer": q["model_answer"],
    }
    return body | {"rubric": rubric, "max_length": 2000}


def ensure_people(spec: dict, state: dict) -> list[str]:
    """the eval's students in keycloak; their passwords stay with the performance eval's, where sign-in reads them."""
    passwords = state.setdefault("performance", {}).setdefault("passwords", {})
    names = [username(n) for n in range(1, students_needed(spec) + 1)]
    with admin_client() as admin:
        for n, name in enumerate(names, 1):
            known = name in passwords
            if not known:
                passwords[name] = secrets.token_hex(20)
            ensure_person(admin, name, "Eval", f"Marker {n}", passwords[name], reset_password=not known)
    save_state(state)
    return names


def ensure_assessments(teacher: Session, spec: dict, state: dict, part: dict, room: str) -> None:
    """one published assessment with ai marking per question; question and criterion ids kept in state."""
    made = part.setdefault("assessments", {})
    topics = part["topics"]
    for key, q in spec["questions"].items():
        if (
            key in made
            and teacher.request("GET", f"/assessment/assessments/{made[key]['id']}").status_code == 200
        ):
            continue
        body = {
            "classroom_id": room,
            "title": spec["prefix"] + key,
            "questions": [question_input(q, topics[q["topic"]])],
            "release_policy": "manual",
            "ai_marking": True,
        }
        created = check(teacher.request("POST", "/assessment/assessments", json=body)).json()
        if created["status"] != "published":
            created = check(
                teacher.request("POST", f"/assessment/assessments/{created['id']}/publish")
            ).json()
        question = created["questions"][0]
        made[key] = {
            "id": created["id"],
            "question": question["id"],
            # a short answer's one criterion is grading's "match".
            "criteria": [c["id"] for c in (question.get("rubric") or {}).get("criteria") or []] or ["match"],
        }
        save_state(state)


def seed(spec: dict, state: dict) -> str:
    part = state.setdefault(SECTION, {})
    teacher, admin = Session("teacher", state), Session("admin", state)
    names = ensure_people(spec, state)
    ids = {n: check(Session(n, state).request("POST", "/identity/me", json={})).json()["id"] for n in names}
    room = ensure_classroom(teacher, admin, spec, state, part)
    for student_id in ids.values():
        check(
            admin.request(
                "POST", f"/identity/admin/classrooms/{room}/enrollments", json={"student_id": student_id}
            )
        )
    part["students"] = ids
    save_state(state)
    ensure_topics(teacher, spec, state, part)
    ensure_assessments(teacher, spec, state, part, room)
    return room


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    spec = fixture()
    room = seed(spec, load_state())
    print(
        f"Marking eval classroom {room} ready: {len(spec['questions'])} assessments, {len(spec['cases'])} cases."
    )


if __name__ == "__main__":
    main()
