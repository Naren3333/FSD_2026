"""delete the marking eval's classroom through identity, so each service purges its own data (submissions, grades and suggestions), and its topics."""

import argparse

from common import load_state
from load_marking import SECTION, fixture
from load_performance import delete_classroom


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    # the people stay, with their passwords, so a later seed signs them in again.
    delete_classroom(fixture(), load_state(), SECTION)


if __name__ == "__main__":
    main()
