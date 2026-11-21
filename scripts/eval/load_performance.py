"""seed the performance eval idempotently: scripted students sit the fixture's assessments, and the teacher marks and finalizes them. no paid calls: ai marking is off."""

import argparse
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from common import Session, load_state, save_state
from local_stack import admin_client, check, delete_topics, ensure_person

FIXTURE = Path(__file__).with_name("performance.yaml")
WAIT = 90  # seconds for an event to reach grading or analytics
WRONG = "Not the expected answer."


def fixture() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def username(key: str) -> str:
    return f"eval-perf-{key}"


def eventually(action, predicate, label: str):
    deadline = time.monotonic() + WAIT
    while True:
        value = action()
        if predicate(value):
            return value
        if time.monotonic() > deadline:
            sys.exit(f"Timed out waiting for {label}; last saw {str(value)[:300]}")
        time.sleep(2)


def ensure_people(spec: dict, state: dict) -> None:
    passwords = state.setdefault("performance", {}).setdefault("passwords", {})
    with admin_client() as admin:
        for key in spec["students"]:
            name = username(key)
            known = name in passwords
            if not known:
                passwords[name] = secrets.token_hex(20)
            ensure_person(
                admin, name, "Eval", key.replace("-", " ").title(), passwords[name], reset_password=not known
            )
    save_state(state)


def ensure_topics(teacher: Session, spec: dict, state: dict, part: dict) -> dict[str, str]:
    """topic name in the fixture → its curriculum id, creating any that are missing, with their prerequisites. `part` is the eval's part of the state."""
    ids: dict[str, str] = part.setdefault("topics", {})
    for name in spec["topics"]:
        if name in ids and teacher.request("GET", f"/curriculum/topics/{ids[name]}").status_code == 200:
            continue
        created = check(teacher.request("POST", "/curriculum/topics", json={"name": spec["prefix"] + name}))
        ids[name] = created.json()["id"]
        save_state(state)
    for name, topic in spec["topics"].items():
        for needed in topic.get("prerequisites") or []:
            res = teacher.request(
                "POST", f"/curriculum/topics/{ids[name]}/prerequisites", json={"prerequisite_id": ids[needed]}
            )
            if res.status_code != 409:  # already linked
                check(res)
    return ids


def ensure_classroom(teacher: Session, admin: Session, spec: dict, state: dict, part: dict) -> str:
    """the teacher's eval classroom, which the school's admin creates for them (adr 0019)."""
    mine = {c["id"] for c in check(teacher.request("GET", "/identity/classrooms")).json()}
    if part.get("classroom") not in mine:
        teacher_id = check(teacher.request("POST", "/identity/me", json={})).json()["id"]
        body = {"name": spec["classroom"], "teacher_id": teacher_id}
        part["classroom"] = check(admin.request("POST", "/identity/admin/classrooms", json=body)).json()["id"]
        part.pop("assessments", None)
        part.pop("students", None)
        save_state(state)
    return str(part["classroom"])


def question_input(q: dict, topic_id: str) -> dict:
    body: dict[str, Any] = {"prompt": q["prompt"], "type": q["type"], "topic_id": topic_id}
    if q["type"] == "multiple_choice":
        body |= {"options": q["options"], "answer": q["answer"]}
    elif q["type"] == "short_answer":
        body |= {"accepted_answers": q["accepted"]}
    else:
        body |= {
            "rubric": {
                "criteria": [
                    {"title": title, "levels": [{"points": p, "descriptor": f"{p} points"} for p in levels]}
                    for title, levels in q["criteria"]
                ],
                "model_answer": q["model_answer"],
            },
            "max_length": 2000,
        }
    return body


def ensure_assessments(
    teacher: Session, spec: dict, state: dict, part: dict, room: str, topics: dict[str, str]
) -> None:
    """each fixture assessment published once; question keys → ids and criteria ids kept in state."""
    made = part.setdefault("assessments", {})
    for key, a in spec["assessments"].items():
        if (
            key in made
            and teacher.request("GET", f"/assessment/assessments/{made[key]['id']}").status_code == 200
        ):
            continue
        keys = list(a["questions"])
        body = {
            "classroom_id": room,
            "title": spec["prefix"] + a["title"],
            "questions": [
                question_input(a["questions"][k], topics[a["questions"][k]["topic"]]) for k in keys
            ],
            "release_policy": "on_finalize",
            "ai_marking": False,
        }
        created = check(teacher.request("POST", "/assessment/assessments", json=body)).json()
        if created["status"] != "published":
            created = check(
                teacher.request("POST", f"/assessment/assessments/{created['id']}/publish")
            ).json()
        made[key] = {
            "id": created["id"],
            "questions": {k: q["id"] for k, q in zip(keys, created["questions"], strict=True)},
            "criteria": {
                k: [c["id"] for c in (q.get("rubric") or {}).get("criteria") or []]
                for k, q in zip(keys, created["questions"], strict=True)
            },
        }
        save_state(state)


def answers_for(spec: dict, key: str, assessment: str) -> dict[str, str]:
    """the question key → answer a student gives on one assessment."""
    student = spec["students"][key]
    wide = spec["class_wide"]["answers"]
    out = {}
    for qkey, q in spec["assessments"][assessment]["questions"].items():
        if qkey in student.get("answers", {}):
            out[qkey] = student["answers"][qkey]
        elif qkey in wide:
            out[qkey] = wide[qkey]
        elif q["type"] == "multiple_choice":
            out[qkey] = q["answer"]
        elif q["type"] == "short_answer":
            out[qkey] = q["accepted"][0]
        else:
            out[qkey] = q["model_answer"]
    return out


def marks_for(spec: dict, key: str, assessment: str, made: dict, evidence: list[dict]) -> dict[str, dict]:
    """the teacher's marks for the questions grading could not mark itself."""
    student, wide = spec["students"][key], spec["class_wide"]
    by_id = {qid: qkey for qkey, qid in made["questions"].items()}
    questions = spec["assessments"][assessment]["questions"]
    out = {}
    for e in evidence:
        if not e.get("needs_marking"):
            continue
        qkey = by_id[e["question_id"]]
        q = questions[qkey]
        given = student.get("marks", {}).get(qkey) or (
            wide["marks"].get(qkey)
            if qkey in wide["answers"] and qkey not in student.get("answers", {})
            else None
        )
        if q["type"] == "free_response":
            levels = given["criteria"] if given else [max(points) for _, points in q["criteria"]]
            mark: dict[str, Any] = {"criteria": dict(zip(made["criteria"][qkey], levels, strict=True))}
        else:
            mark = {"awarded": given["awarded"] if given else 0}
        feedback = given.get("feedback") if given else (None if q["type"] == "free_response" else WRONG)
        if feedback:
            mark["feedback"] = feedback
        out[e["question_id"]] = mark
    return out


def sit_and_mark(
    teacher: Session, spec: dict, state: dict, part: dict, room: str, ids: dict[str, str]
) -> None:
    for key, student in spec["students"].items():
        session = Session(username(key), state)
        for assessment in student["sits"]:
            made = part["assessments"][assessment]
            path = f"/assessment/assessments/{made['id']}"
            if session.request("GET", f"{path}/submission").status_code == 404:
                answers = {made["questions"][k]: v for k, v in answers_for(spec, key, assessment).items()}
                check(session.request("POST", f"{path}/submissions", json={"answers": answers}))
        listed = f"/grading/evaluations?classroom_id={room}&student_id={ids[key]}"
        page = eventually(
            lambda listed=listed: check(teacher.request("GET", listed)).json(),
            lambda p, n=len(student["sits"]): p["total"] >= n,
            f"{key}'s draft grades",
        )
        for v in page["evaluations"]:
            if v["status"] == "finalized":
                continue
            assessment = next(k for k, m in part["assessments"].items() if m["id"] == v["assessment_id"])
            marks = marks_for(spec, key, assessment, part["assessments"][assessment], v["evidence"])
            check(
                teacher.request(
                    "POST",
                    f"/grading/evaluations/{v['id']}/finalize",
                    json={"reason": "Performance eval fixture", "marks": marks},
                )
            )
        print(f"  {key}: {len(student['sits'])} assessments finalized")


def delete_classroom(spec: dict, state: dict, section: str, keep: tuple[str, ...] = ()) -> None:
    """the section's classroom through identity, so each service purges its own data, and the eval's topics; `keep` names the state the section keeps."""
    part = state.get(section, {})
    if room := part.get("classroom"):
        res = Session("teacher", state).request("DELETE", f"/identity/classrooms/{room}")
        if res.status_code != 404:  # already deleted is fine
            check(res)
        print(f"deleted: {spec['classroom']}")
    if part.get("topics"):
        prefix = spec["prefix"].replace("'", "''")
        print(f"deleted {delete_topics(f"name LIKE '{prefix}%'")} eval topics")
    state[section] = {k: part.get(k, {}) for k in keep}
    save_state(state)


def seed(spec: dict, state: dict, section: str) -> tuple[str, dict[str, str]]:
    """the spec's students enrolled in the eval's classroom, the topics and assessments made, every sitting marked and finalized, and analytics caught up; the classroom and student ids are kept under `section` of the state."""
    part = state.setdefault(section, {})
    teacher, admin = Session("teacher", state), Session("admin", state)
    ensure_people(spec, state)  # passwords stay with the performance eval's, where sign-in reads them
    ids = {
        key: check(Session(username(key), state).request("POST", "/identity/me", json={})).json()["id"]
        for key in spec["students"]
    }
    room = ensure_classroom(teacher, admin, spec, state, part)
    for student_id in ids.values():
        check(
            admin.request(
                "POST", f"/identity/admin/classrooms/{room}/enrollments", json={"student_id": student_id}
            )
        )
    part["students"] = ids
    save_state(state)
    topics = ensure_topics(teacher, spec, state, part)
    ensure_assessments(teacher, spec, state, part, room, topics)
    sit_and_mark(teacher, spec, state, part, room, ids)
    # analytics counts what finalization released; wait until it has every student who sat something.
    for key, student in spec["students"].items():
        sat = {q["topic"] for a in student["sits"] for q in spec["assessments"][a]["questions"].values()}
        if sat:
            eventually(
                lambda k=key: check(
                    teacher.request("GET", f"/analytics/performance/{ids[k]}?classroom_id={room}")
                ).json()["topics"],
                lambda t, n=len(sat): len(t) == n,
                f"{key} in analytics",
            )
    return room, ids


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    room, ids = seed(fixture(), load_state(), "performance")
    print(f"Performance eval classroom {room} ready with {len(ids)} students.")


if __name__ == "__main__":
    main()
