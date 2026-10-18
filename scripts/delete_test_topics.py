"""Delete the topics earlier test runs left in the local curriculum.

    python scripts/delete_test_topics.py           # lists what it would delete
    python scripts/delete_test_topics.py --apply   # deletes them

Before 2026-10-17, scripts/integration.py and the browser tests created topics (then called skills) on every run and
never removed them: "Integration exact decimals", "Integration photosynthesis", "Integration question generation",
"Browser skill <timestamp>" and "Testing skill <timestamp>". Since then each run deletes its own; since 2026-10-18 the
browser tests name theirs "Browser topic <timestamp>" and "Testing topic <timestamp>". This deletes the older ones,
archived or not, straight from Curriculum's database in the Compose stack, with any prerequisite links to them. Only
names that match exactly are touched. Test classrooms that used them show their id where the name was. Local
development stack only.
"""

import argparse

from local_stack import delete_topics, psql_json

# The browser tests' names end in a 13-digit millisecond timestamp, which a hand-typed name is unlikely to have. The
# old "skill" names are kept so topics left before the rename are found too.
TEST_TOPICS = (
    "name IN ('Integration exact decimals','Integration photosynthesis','Integration question generation') "
    "OR name ~ '^(Browser|Testing) (skill|topic) [0-9]{13}$'"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="delete them; without it, only list them")
    apply = parser.parse_args().apply
    found = psql_json(
        f"SELECT name, count(*) AS n FROM topics WHERE {TEST_TOPICS} GROUP BY name ORDER BY name",
        database="curriculum",
        user="curriculum",
    )
    total = sum(row["n"] for row in found)
    for row in found:
        print(f"{'deleting' if apply else 'would delete'}: {row['name']} ({row['n']})")
    if not apply:
        print(f"{total} test topics found. Run again with --apply to delete them.")
        return
    total = int(delete_topics(TEST_TOPICS))
    print(f"Deleted {total} test topics.")


if __name__ == "__main__":
    main()
