"""Delete the classrooms earlier test runs left in the local stack.

    python scripts/delete_test_classrooms.py                 # lists what it would delete
    python scripts/delete_test_classrooms.py --apply         # deletes them
    python scripts/delete_test_classrooms.py --eval --apply  # the golden eval's classrooms too

Before 2026-10-18 the browser tests created a classroom on every run and never deleted it ("Browser", "Chat",
"Mocked chat" and "Generation" with a 13-digit millisecond timestamp), and a failed scripts/integration.py run left
its "Integration <8 hex digits>" classroom behind. Since then each run deletes its own. This deletes the older ones
as the local `teacher`, through Identity's API like the Delete button on the Classrooms page, so every service purges
its data for them. Only names that match exactly are touched.

The golden eval's "Golden eval C1" and "Golden eval C2" (scripts/eval/load_corpus.py) are kept for re-running the
eval, since indexing their documents again costs embeddings; --eval deletes them as well and forgets their ids, and
load_corpus.py creates them again. Local development stack only; needs httpx.
"""

import argparse
import re
import sys
from pathlib import Path

import httpx
from local_stack import BASE, check, env, password_token

TEST_CLASSROOMS = re.compile(r"(Browser|Chat|Mocked chat|Generation) [0-9]{13}|Integration [0-9a-f]{8}")
EVAL_CLASSROOMS = re.compile(r"Golden eval C[12]")


def matches(name: str, include_eval: bool) -> bool:
    return bool(TEST_CLASSROOMS.fullmatch(name) or (include_eval and EVAL_CLASSROOMS.fullmatch(name)))


def forget_eval_classrooms() -> None:
    """Drops the deleted eval classrooms and their documents from the eval's state, keeping the second student's
    password, so the eval scripts don't search classrooms that are gone."""
    sys.path.insert(0, str(Path(__file__).with_name("eval")))
    from common import load_state, save_state  # noqa: PLC0415 - only --eval needs the eval's state

    state = load_state()
    if "classrooms" in state or "documents" in state:
        state.pop("classrooms", None)
        state.pop("documents", None)
        save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="delete them; without it, only list them")
    parser.add_argument("--eval", action="store_true", help='also "Golden eval C1" and "Golden eval C2"')
    args = parser.parse_args()
    token = password_token("teacher", env()["TEACHER_PASSWORD"])
    deleted = eval_deleted = 0
    with httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + token}, timeout=25) as teacher:
        while True:
            # Identity lists a teacher's 100 newest classrooms; older ones show up as newer ones are deleted.
            listed = check(teacher.get("/identity/classrooms")).json()
            found = [c for c in listed if matches(c["name"], args.eval)]
            more = " (or more: only the 100 newest classrooms are listed)" if len(listed) >= 100 else ""
            if not args.apply:
                for c in found:
                    print(f"would delete: {c['name']}")
                print(f"{len(found)} test classrooms found{more}. Run again with --apply to delete them.")
                return
            if not found:
                break
            for c in found:
                check(teacher.delete(f"/identity/classrooms/{c['id']}"))
                print(f"deleted: {c['name']}")
                eval_deleted += bool(EVAL_CLASSROOMS.fullmatch(c["name"]))
            deleted += len(found)
    # Only what the eval's state names was deleted, so load_corpus.py creates them again rather than twice.
    if eval_deleted:
        forget_eval_classrooms()
    print(f"Deleted {deleted} test classrooms.")
    if more:
        print(
            "Older test classrooms may remain behind the 100 newest, which Identity lists; delete newer ones first."
        )


if __name__ == "__main__":
    main()
