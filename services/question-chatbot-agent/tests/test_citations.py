import logging

import pytest

from citations import HOLD, CitationRewriter

TAGS = {f"S{n}": f"c{n}" for n in range(1, 13)}


def run(deltas: list[str], tags: dict[str, str] = TAGS) -> tuple[str, CitationRewriter]:
    """Feed the deltas one by one, as the stream does, and return everything relayed."""
    r = CitationRewriter(tags)
    return "".join(r.feed(d) for d in deltas) + r.flush(), r


def test_whole_tag() -> None:
    out, r = run(["Light is absorbed [S1]."])
    assert out == "Light is absorbed [doc:c1]."
    assert r.cited == ["c1"]


@pytest.mark.parametrize(
    "deltas", [["Light [", "S", "1", "] ok"], ["Light [S", "1] ok"], list("Light [S1] ok")]
)
def test_tag_split_across_deltas(deltas: list[str]) -> None:
    assert run(deltas)[0] == "Light [doc:c1] ok"


def test_split_tag_is_held_back_not_relayed_half_done() -> None:
    r = CitationRewriter(TAGS)
    assert r.feed("Light [S") == "Light "
    assert r.feed("1] ok") == "[doc:c1] ok"


def test_two_digit_tag() -> None:
    out, r = run(["see [S1", "2]"])
    assert out == "see [doc:c12]" and r.cited == ["c12"]


def test_unknown_tag_is_dropped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, "question-chatbot-agent.citations"):
        out, r = run(["Fact [S9]."], {"S1": "c1"})
    assert out == "Fact ." and r.cited == []
    assert [(r.getMessage(), getattr(r, "label", None)) for r in caplog.records] == [
        ("citation_dropped_unknown_source", "S9")
    ]


@pytest.mark.parametrize("tag", ["[S1, S3]", "[S1,S3]", "[S1; S3]"])
def test_comma_list_becomes_one_marker_each(tag: str) -> None:
    out, r = run([f"Both {tag[:4]}", f"{tag[4:]}."])
    assert out == "Both [doc:c1][doc:c3]." and r.cited == ["c1", "c3"]


def test_list_with_an_unknown_tag_keeps_the_known_one() -> None:
    assert run(["x [S1, S9]"], {"S1": "c1"})[0] == "x [doc:c1]"


@pytest.mark.parametrize("tag", ["[S1-S3]", "[S1–3]"])
def test_range_is_dropped(tag: str) -> None:
    out, r = run([f"x {tag} y"])
    assert out == "x  y" and r.cited == []


def test_cited_is_ordered_and_unique() -> None:
    assert run(["[S2] a [S1] b [S2]"])[1].cited == ["c2", "c1"]


@pytest.mark.parametrize(
    "text", ["a [note] b", "array[0]", "[Simple](link)", "x [S] y", "1 [ 2", "[S1 is great"]
)
def test_brackets_that_arent_tags_pass_through(text: str) -> None:
    assert run(list(text))[0] == text


def test_long_unclosed_run_is_released_at_hold() -> None:
    r = CitationRewriter(TAGS)
    run_ = "[S1" + ", S1" * 10  # tag-like, never closed
    out = r.feed(run_)
    assert len(out) >= len(run_) - HOLD and run_.startswith(out)
    assert out + r.flush() == run_


def test_tag_at_the_end_of_the_stream() -> None:
    assert run(["The end ", "[S1]"])[0] == "The end [doc:c1]"


def test_unclosed_tag_at_the_end_is_flushed_as_written() -> None:
    assert run(["The end [S1"])[0] == "The end [S1"


def test_bracket_after_a_failed_tag_starts_a_new_one() -> None:
    assert run(["[x[S1]"])[0] == "[x[doc:c1]"
