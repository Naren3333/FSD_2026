"""delete the eval classrooms through authorized apis so each service purges its own data."""

import argparse

from common import Session, load_state, save_state
from local_stack import check


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    state = load_state()
    classrooms = state.get("classrooms", {})
    if not classrooms:
        print("No eval classrooms recorded; nothing to delete.")
        return
    teacher = Session("teacher", state)
    for name, room in classrooms.items():
        res = teacher.request("DELETE", f"/identity/classrooms/{room}")
        if res.status_code != 404:  # already deleted is fine
            check(res)
        print(f"deleted: Golden eval {name}")
    state.pop("classrooms", None)
    state.pop("documents", None)
    save_state(state)


if __name__ == "__main__":
    main()
