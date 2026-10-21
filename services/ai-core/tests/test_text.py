"""Markdown and plain text: paragraphs, headings and tables for the chunker, and POST /v1/ingest with them."""

from conftest import FakeOpenAI, ingested
from fastapi.testclient import TestClient

from ingest.chunker import chunk_paras
from ingest.text import paragraphs

NOTES = """---
title: Testing notes
---

# Software Testing

Testing finds defects before users do.

## Unit tests

A unit test checks one function in isolation.
- fast
- repeatable

```python
# not a heading
def test_add():
    assert add(1, 2) == 3
```

Levels compared
===============

The levels differ in scope. They compare as follows:

| Level | Scope |
|---|:---:|
| Unit | One function |
| System | The whole app \\| deployed |

***

Closing words.
"""


def test_markdown_headings_set_the_section_path() -> None:
    paras = paragraphs(NOTES, markup=True)
    headings = [(p.text, p.level) for p in paras if p.level is not None]
    # setext "===" is a level-1 heading, like "#"; ranked by the levels used
    assert headings == [("Software Testing", 0), ("Unit tests", 1), ("Levels compared", 0)]
    body = {p.text.split("\n")[0]: p.path for p in paras if p.level is None and not p.region}
    assert body["Testing finds defects before users do."] == ("Software Testing",)
    assert body["A unit test checks one function in isolation."] == ("Software Testing", "Unit tests")
    assert body["Closing words."] == ("Levels compared",)
    assert "title: Testing notes" not in "\n".join(p.text for p in paras)


def test_a_lone_title_is_text_so_its_sections_are_the_top_level() -> None:
    paras = paragraphs("# Handbook\n\nIntro.\n\n## Setup\n\nInstall it.\n\n## Use\n\nRun it.", markup=True)
    assert [(p.text, p.level, p.path) for p in paras] == [
        ("Handbook", None, ()),
        ("Intro.", None, ()),
        ("Setup", 0, ("Setup",)),
        ("Install it.", None, ("Setup",)),
        ("Use", 0, ("Use",)),
        ("Run it.", None, ("Use",)),
    ]


def test_markdown_keeps_lists_and_code_blocks_whole() -> None:
    paras = paragraphs(NOTES, markup=True)
    assert "A unit test checks one function in isolation.\n- fast\n- repeatable" in [p.text for p in paras]
    [code] = [p for p in paras if p.text.startswith("```")]
    assert "# not a heading" in code.text and code.text.endswith("```") and code.level is None


def test_a_pipe_table_is_a_table_captioned_by_the_sentence_before_it() -> None:
    [para] = [p for p in paragraphs(NOTES, markup=True) if p.table]
    table = para.table
    assert table is not None
    assert table.header == ("Level", "Scope")
    assert table.rows == (("Unit", "One function"), ("System", "The whole app | deployed"))
    assert table.caption == "They compare as follows:" and para.path == ("Levels compared",)
    [chunk] = [c for c in chunk_paras(paragraphs(NOTES, markup=True)) if c.kind == "table"]
    assert "| Unit | One function |" in chunk.content
    assert chunk.embed_body and "Level: System; Scope: The whole app | deployed" in chunk.embed_body


def test_plain_text_is_paragraphs_only() -> None:
    paras = paragraphs("# not a heading\nline two\n\n\n| a | b |\n|---|---|\n", markup=False)
    assert [(p.text, p.level, p.table) for p in paras] == [
        ("# not a heading\nline two", None, None),
        ("| a | b |\n|---|---|", None, None),
    ]


def test_markdown_is_indexed_on_one_page_with_section_paths(api: TestClient, embeddings: FakeOpenAI) -> None:
    doc = ingested(api, NOTES.encode(), content_type="text/markdown")
    assert doc["outcome"] == "indexed" and doc["page_count"] == 1 and doc["figures"] == []
    chunks = doc["chunks"]
    assert all(c["page_start"] == c["page_end"] == 1 and c["source"] == "pdf" for c in chunks)
    assert {c["kind"] for c in chunks} == {"text", "table"}
    # a chunk never crosses a top-level section; its path is what its paragraphs share
    assert [c["section_path"] for c in chunks] == ["Software Testing", "Levels compared", "Levels compared"]
    assert chunks[0]["embed_text"].startswith(
        "Biology\nSoftware Testing\n\nSoftware Testing\n\nTesting finds"
    )


def test_plain_text_is_indexed(api: TestClient, embeddings: FakeOpenAI) -> None:
    doc = ingested(
        api, "﻿Photosynthesis makes sugar.\n\nRespiration uses it.".encode(), content_type="text/plain"
    )
    [chunk] = doc["chunks"]
    assert chunk["content"] == "Photosynthesis makes sugar.\n\nRespiration uses it."
    assert chunk["section_path"] is None


def test_text_that_is_not_utf8_fails_corrupt(api: TestClient) -> None:
    doc = ingested(api, b"caf\xe9", content_type="text/plain")
    assert (doc["outcome"], doc["failure_reason"]) == ("failed", "CORRUPT")


def test_blank_text_has_nothing_to_index(api: TestClient) -> None:
    doc = ingested(api, b"\n\n   \n", content_type="text/markdown")
    assert (doc["outcome"], doc["failure_reason"]) == ("failed", "NO_TEXT_LAYER")
