"""verify that compose provisions every topic the services use."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TOPIC = re.compile(r"[\"']([a-z]+(?:\.[a-z_]+)+\.v[0-9]+)[\"']")
# these content events have no consumer yet, so they have no dead-letter topic. (content.deleted.v1 has one
# since adr 0010: the chatbot redacts the deleted document's citations.)
EMIT_ONLY = {"content.uploaded.v1", "content.indexed.v1"}


def created_topics() -> set[str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    command = " ".join(compose["services"]["kafka-init"]["command"])
    loop = re.search(r"for topic in (.*?); do", command, re.DOTALL)
    assert loop, "kafka-init no longer creates its topics in one for loop"
    return set(loop.group(1).split())


def sources() -> list[Path]:
    """the services' and shared packages' go and python source, tests left out."""
    found = []
    for root in [ROOT / "services", ROOT / "packages"]:
        for path in root.rglob("*"):
            test = path.name.endswith("_test.go") or path.name.startswith("test_") or "tests" in path.parts
            if path.suffix in {".go", ".py"} and not test and "node_modules" not in path.parts:
                found.append(path)
    return found


def named_topics() -> dict[str, set[str]]:
    """each topic the source names, with the files naming it."""
    topics: dict[str, set[str]] = {}
    for path in sources():
        for topic in TOPIC.findall(path.read_text(encoding="utf-8")):
            topics.setdefault(topic, set()).add(path.relative_to(ROOT).as_posix())
    return topics


def test_compose_creates_every_topic_the_services_name():
    named = named_topics()
    assert "assessment.deleted.v1" in named and "grading.finalized.v3" in named  # the scan finds them
    assert set(named) - created_topics() == set()


def test_compose_creates_no_topic_the_services_no_longer_name():
    """retired event versions leave compose too, with their dead-letter topics."""
    named = named_topics()
    unused = {t for t in created_topics() if t.removesuffix(".dlq") not in named}
    assert unused == set()


def test_every_consumed_topic_has_its_dead_letter_topic():
    created = created_topics()
    missing = {f"{topic}.dlq" for topic in named_topics() if topic not in EMIT_ONLY} - created
    assert missing == set()


def test_emit_only_topics_are_only_named_by_content():
    """a service that starts consuming one of content's events needs its dead-letter topic: move it out of emit_only."""
    named = named_topics()
    elsewhere = {
        t: files
        for t, files in named.items()
        if t in EMIT_ONLY and any(not f.startswith("services/content/") for f in files)
    }
    assert elsewhere == {}
