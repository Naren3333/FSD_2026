"""delete the performance eval's classroom through identity, so each service purges its own data, and its topics."""

import argparse

from common import Session, load_state, save_state
from load_performance import fixture
from local_stack import check, delete_topics


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    state = load_state()
    perf = state.get("performance", {})
    if room := perf.get("classroom"):
        res = Session("teacher", state).request("DELETE", f"/identity/classrooms/{room}")
        if res.status_code != 404:  # already deleted is fine
            check(res)
        print(f"deleted: {fixture()['classroom']}")
    if perf.get("topics"):
        prefix = fixture()["prefix"].replace("'", "''")
        print(f"deleted {delete_topics(f"name LIKE '{prefix}%'")} eval topics")
    # the people stay, with their passwords, so a later seed signs them in again.
    state["performance"] = {"passwords": perf.get("passwords", {})}
    save_state(state)


if __name__ == "__main__":
    main()
