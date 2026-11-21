"""seed the class insights eval idempotently: class_insights.yaml's students (or --spec's) sit the performance eval's assessments in their own classroom, and the teacher marks and finalizes them. no paid calls: ai marking is off. --delete removes the classroom and its topics instead."""

import argparse
from pathlib import Path
from typing import Any

import yaml
from common import Session, load_state, service_modules
from load_performance import delete_classroom, seed
from load_performance import fixture as performance_fixture
from local_stack import check

FIXTURE = Path(__file__).with_name("class_insights.yaml")
SECTION = "class_insights"  # the default spec's part of the state; another spec names its own `section`

# the agent's own rule for who struggles, so the eval's truth cannot drift from it.
(class_workspace,) = service_modules("student-performance-agent", "class_workspace")


def fixture(path: Path = FIXTURE) -> dict[str, Any]:
    """a spec's students and cases, with the performance eval's topics, assessments and class-wide answers, and the state section it is kept under."""
    own = yaml.safe_load(path.read_text(encoding="utf-8"))
    shared = performance_fixture()
    return {"section": SECTION, **own, **{k: shared[k] for k in ("topics", "assessments", "class_wide")}}


def truth_of(teacher: Session, room: str) -> dict[tuple[str, str], str]:
    """(student id, topic id) → what analytics' released figures support: struggling, insufficient or fine."""
    page = analytics_page(teacher, room)
    ws = class_workspace.ClassWorkspace()
    ws.add_class([], page["students"], page["total"])
    return {(s.student_id, t): ws.standing(s.student_id, t) for s in ws.students.values() for t in s.topics}


def analytics_page(teacher: Session, room: str) -> dict:
    return check(teacher.request("GET", f"/analytics/performance?classroom_id={room}&limit=50")).json()


def mismatches(spec: dict, truth: dict, keys: dict[str, str], names: dict[str, str]) -> list[str]:
    """where analytics' standings differ from what the spec meant: a student it meant to struggle who does not, or one who struggles unmeant. `keys` maps student id → fixture key, `names` topic id → fixture name."""
    meant = {(topic, key) for topic, ks in spec["expect"]["struggling"].items() for key in ks}
    found = {(names.get(t, t), keys.get(s, s)) for (s, t), v in truth.items() if v == "struggling"}
    return [
        f"{k} meant to struggle with {t}, analytics shows {_standing(truth, keys, names, k, t)}"
        for t, k in sorted(meant - found)
    ] + [f"{k} struggles with {t}, not meant to" for t, k in sorted(found - meant)]


def _standing(truth: dict, keys: dict[str, str], names: dict[str, str], key: str, topic: str) -> str:
    return next((v for (s, t), v in truth.items() if keys.get(s) == key and names.get(t) == topic), "none")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delete", action="store_true", help="delete the eval's classroom and topics")
    ap.add_argument("--spec", type=Path, default=FIXTURE, help=f"the class to seed (default {FIXTURE.name})")
    args = ap.parse_args()
    spec, state = fixture(args.spec), load_state()
    if args.delete:
        delete_classroom(spec, state, spec["section"])
        return
    room, ids = seed(spec, state, spec["section"])
    print(f"{spec['classroom']} classroom {room} ready with {len(ids)} students.")
    part = state[spec["section"]]
    truth = truth_of(Session("teacher", state), room)
    keys, names = {v: k for k, v in ids.items()}, {v: k for k, v in part["topics"].items()}
    for line in mismatches(spec, truth, keys, names):
        print(f"  differs from the spec: {line}")


if __name__ == "__main__":
    main()
