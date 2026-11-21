"""delete the performance eval's classroom through identity, so each service purges its own data, and its topics."""

import argparse

from common import load_state
from load_performance import delete_classroom, fixture


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    # the people stay, with their passwords, so a later seed signs them in again.
    delete_classroom(fixture(), load_state(), "performance", keep=("passwords",))


if __name__ == "__main__":
    main()
