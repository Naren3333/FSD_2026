"""The structure-aware chunker. No DB, no OpenAI."""

from itertools import pairwise

from ingest.chunker import MAX_TOKENS, SEPARATOR, chunk_paras, encoding
from ingest.headings import Para

SENTENCE = "Plants turn light energy into chemical energy stored in glucose molecules. "


def paragraph(n: int, sentences: int = 4) -> str:
    return f"Paragraph {n}. " + SENTENCE * sentences  # ~60 tokens


def book(pages: int = 3, per_page: int = 25) -> list[list[str]]:
    return [[paragraph(p * per_page + i).strip() for i in range(per_page)] for p in range(pages)]


def plain(pages: list[list[str]]) -> list[Para]:
    """Body paragraphs with no headings, as from a PDF without any."""
    return [Para(text, page_no, ()) for page_no, texts in enumerate(pages, start=1) for text in texts]


class Doc:
    """Builds an outline the way headings.outline would: a path stack per heading level."""

    def __init__(self) -> None:
        self.paras: list[Para] = []
        self.stack: list[tuple[int, str]] = []

    def h(self, level: int, text: str, page: int = 1) -> "Doc":
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, text))
        self.paras.append(Para(text, page, tuple(t for _, t in self.stack), level))
        return self

    def p(self, text: str, page: int = 1, count: int = 1) -> "Doc":
        self.paras += [Para(text, page, tuple(t for _, t in self.stack))] * count
        return self


def tokens(text: str) -> int:
    return len(encoding().encode(text))


def test_chunks_fit_the_token_budget() -> None:
    chunks = chunk_paras(plain(book()))
    assert len(chunks) >= 4
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(c.token_count <= MAX_TOKENS for c in chunks)
    assert all(c.token_count >= 400 for c in chunks[:-1])
    assert all(c.token_count == tokens(c.content) for c in chunks)
    assert all(c.section_path is None for c in chunks)


def test_consecutive_chunks_overlap_by_a_paragraph() -> None:
    chunks = chunk_paras(plain(book()))
    for prev, nxt in pairwise(chunks):
        assert prev.content.split(SEPARATOR)[-1] == nxt.content.split(SEPARATOR)[0]
    # ...and nothing else repeats: every paragraph appears, in order.
    seen = [p for c in chunks for p in c.content.split(SEPARATOR)]
    assert list(dict.fromkeys(seen)) == [p for page in book() for p in page]


def test_paragraphs_over_the_overlap_budget_still_overlap() -> None:
    chunks = chunk_paras(plain([[paragraph(n, sentences=8).strip() for n in range(20)]]))  # ~110 tokens each
    for prev, nxt in pairwise(chunks):
        carried = nxt.content.split(SEPARATOR)[0]
        assert prev.content.endswith(carried) and not carried.startswith("Paragraph")
        assert 40 <= tokens(carried) < 90
        assert carried.startswith("Plants")  # cut at a sentence start
    assert all(c.token_count <= MAX_TOKENS for c in chunks)


def test_page_ranges_follow_the_paragraphs() -> None:
    chunks = chunk_paras(plain(book()))
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 3
    assert any(c.page_start < c.page_end for c in chunks)  # some chunk crosses a page break
    for prev, nxt in pairwise(chunks):
        assert prev.page_start <= nxt.page_start and nxt.page_start <= prev.page_end + 1


def test_an_oversized_paragraph_is_split_at_sentence_ends() -> None:
    chunks = chunk_paras(plain([[paragraph(1, sentences=110)]]))  # ~1500 tokens in one block
    assert len(chunks) == 3
    assert all(c.token_count <= MAX_TOKENS for c in chunks)
    assert all(c.page_start == c.page_end == 1 for c in chunks)
    pieces = [c.content.split(SEPARATOR)[-1] for c in chunks]  # each chunk's own (non-overlap) piece
    assert all(p.endswith("molecules.") for p in pieces)
    assert all(p.startswith(("Paragraph", "Plants")) for p in pieces)


def test_a_giant_sentence_falls_back_to_token_windows() -> None:
    sentence = "word " * 1500 + "end."  # ~1500 tokens, no sentence break
    chunks = chunk_paras(plain([[sentence]]))
    assert len(chunks) >= 3 and all(c.token_count <= MAX_TOKENS for c in chunks)


def test_no_text_no_chunks() -> None:
    assert chunk_paras([]) == []


def test_chunks_never_cross_a_top_level_section() -> None:
    doc = (
        Doc()
        .h(0, "Chapter 1")
        .p(paragraph(1), count=3)  # ~190 tokens: room for more, but the chapter ends
        .h(0, "Chapter 2", page=2)
        .p(paragraph(2), page=2, count=12)
    )
    chunks = chunk_paras(doc.paras)
    assert chunks[0].content.startswith("Chapter 1") and "Chapter 2" not in chunks[0].content
    assert chunks[0].section_path == "Chapter 1"
    assert chunks[1].content.startswith("Chapter 2")  # no overlap carried across the boundary
    assert all(c.section_path == "Chapter 2" for c in chunks[1:])
    assert (chunks[1].page_start, chunks[0].page_end) == (2, 1)


def test_a_chunk_spanning_subsections_gets_their_common_prefix() -> None:
    doc = (
        Doc()
        .h(0, "Chapter 3")
        .h(1, "Photosynthesis")
        .h(2, "Light reactions")
        .p(paragraph(1), count=2)
        .h(2, "Calvin cycle")
        .p(paragraph(2), count=2)
    )
    [chunk] = chunk_paras(doc.paras)
    assert chunk.section_path == "Chapter 3 > Photosynthesis"


def test_carried_overlap_does_not_widen_the_path() -> None:
    # ~83-token paragraphs: seven fill chunk 0 so that "Calvin cycle" + its first paragraph don't fit.
    doc = (
        Doc()
        .h(0, "Chapter 3")
        .h(1, "Light reactions")
        .p(paragraph(1, sentences=6), count=7)
        .h(1, "Calvin cycle")
        .p(paragraph(2, sentences=6), count=4)
    )
    first, second = chunk_paras(doc.paras)
    assert first.section_path == "Chapter 3 > Light reactions"
    carried, heading = second.content.split(SEPARATOR)[:2]
    # The chunk opens with the end of a "Light reactions" paragraph but is labelled by its own content.
    assert first.content.endswith(carried) and heading == "Calvin cycle"
    assert second.section_path == "Chapter 3 > Calvin cycle"


def test_no_chunk_ends_with_a_heading() -> None:
    doc = Doc().h(0, "Chapter 1")
    for n in range(6):
        doc.h(1, f"Topic {n}").p(paragraph(n, sentences=6), count=2)  # ~200 tokens per topic
    chunks = chunk_paras(doc.paras)
    headings = {p.text for p in doc.paras if p.level is not None}
    assert len(chunks) >= 2
    assert all(c.content.split(SEPARATOR)[-1] not in headings for c in chunks)
    assert all(c.token_count <= MAX_TOKENS for c in chunks)
    seen = [p for c in chunks for p in c.content.split(SEPARATOR)]
    assert all(h in seen for h in headings)  # moved, not lost


def test_a_title_page_joins_the_first_section() -> None:
    # A slide deck: the course title (demoted to body text) sits before the first slide title.
    doc = Doc().p("Collaborative Software Development")
    doc.paras += Doc().h(0, "Software Testing").p("Software testers succeed where others fail.").paras
    doc.paras += Doc().h(0, "Test-Driven Development", page=2).p(paragraph(1), page=2).paras
    first, second = chunk_paras(doc.paras)
    assert first.content.startswith("Collaborative Software Development\n\nSoftware Testing")
    assert first.section_path == "Software Testing"  # the title line under no heading doesn't wipe the path
    assert second.section_path == "Test-Driven Development" and second.page_start == 2


def test_embed_text_uses_only_the_deepest_heading() -> None:
    from ingest import embed_text
    from ingest.chunker import Chunk

    assert (
        embed_text("NIST", Chunk(0, "Body", 1, 1, 1, "Part 1 > 3. Trustworthiness > Fair"))
        == "NIST\nFair\n\nBody"
    )
    assert embed_text("NIST", Chunk(0, "Body", 1, 1, 1, None)) == "NIST\n\nBody"


def test_outline_sections_split_at_the_second_level() -> None:
    """An outline's top level is often a few parts; its sections bound chunks too."""
    from ingest.chunker import _sections

    paras = [
        Para("Part 1", 1, ("Part 1",), 0, toc=True),
        Para("1. Risk", 1, ("Part 1", "1. Risk"), 1, toc=True),
        Para("Text about risk.", 1, ("Part 1", "1. Risk")),
        Para("2. Audience", 2, ("Part 1", "2. Audience"), 1, toc=True),
        Para("Text about the audience.", 2, ("Part 1", "2. Audience")),
    ]
    assert [[p.text for p in s] for s in _sections(paras)] == [
        ["Part 1", "1. Risk", "Text about risk."],
        ["2. Audience", "Text about the audience."],
    ]
    by_size = [Para(p.text, p.page, p.path, p.level) for p in paras]
    assert len(_sections(by_size)) == 1


def test_captions_give_labels_and_text_gives_citations() -> None:
    """What search uses to hand back the figure a paragraph mentions."""
    from ingest.chunker import cited, label_of

    assert label_of("Fig. 7. The data efficiency") == "figure 7" and label_of("Table 2: Results") == "table 2"
    assert label_of("Figures and their meaning") is None and label_of(None) is None
    assert cited("As shown in Figure 7 and Table 2, and again in fig. 7a (see Figure 7).") == (
        "figure 7",
        "table 2",
        "figure 7a",
    )
    [chunk] = chunk_paras([Para("As Figure 3 shows, the loss falls.", 1, ())])
    assert chunk.cites == ("figure 3",) and chunk.label is None


def test_a_title_only_slide_with_a_table_keeps_just_the_table() -> None:
    from ingest.tables import Table

    table = Table((0, 0, 100, 100), ("Week", "Topic"), (("1", "Software processes"),))
    paras = [
        Para("Learning Journey", 2, ("Learning Journey",), 0),
        Para("Week | Topic", 2, ("Learning Journey",), table=table),
        Para(paragraph(1), 3, ("Learning Journey",)),
    ]
    chunks = chunk_paras(paras)
    assert [c.kind for c in chunks] == ["table", "text"]
    assert [c.ordinal for c in chunks] == [0, 1]


def test_a_short_chunk_alone_on_its_page_is_kept() -> None:
    chunks = chunk_paras([Para("Thank you", 1, ())])
    assert [c.content for c in chunks] == ["Thank you"]
