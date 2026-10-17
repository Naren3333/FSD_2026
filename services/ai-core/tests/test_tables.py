"""Tables: extraction from the text layer, the parser, the outline and the chunker.
No DB, no OpenAI: the vision pass's results are given directly."""

from pathlib import Path
from typing import Any

import pymupdf
from conftest import FakeOpenAI, make_table_pdf

from ingest import embed_text
from ingest.chunker import CONTINUED, MAX_TOKENS, TRANSCRIBED, chunk_paras, encoding
from ingest.geometry import Box
from ingest.headings import Para, outline
from ingest.parse import Block, _join_continued, read_pages
from ingest.strings import clean_cell, unligature
from ingest.tables import Table, _header_rows, markdown, page_tables
from ingest.vision import PageVision, SeenTable

HEADER = ["Criterion", "Band 4", "Band 2"]
RUBRIC = [
    ["Analysis", "Explains causes clearly", "Lists causes"],
    ["Evidence", "Cites three sources", "Cites one source"],
    ["Structure", "Logical sections", "Some order"],
]
INTRO = "Each criterion is marked in bands. The rubric below shows what each band requires."


def page_of(tmp_path: Path, data: bytes) -> pymupdf.Page:
    path = tmp_path / "t.pdf"
    path.write_bytes(data)
    return pymupdf.open(path)[0]


def seen(box: Box, rows: int, cols: int, style: str = "bordered", **kw: Any) -> SeenTable:
    return SeenTable(box, rows, cols, style, **kw)


# ---- extraction


def test_a_ruled_table_is_found_without_vision(tmp_path: Path) -> None:
    data, _ = make_table_pdf(HEADER, RUBRIC)
    [table] = page_tables(page_of(tmp_path, data), None).tables
    assert table.header == tuple(HEADER) and table.rows == tuple(tuple(r) for r in RUBRIC)
    assert table.source == "pdf"


def test_a_borderless_table_is_read_inside_the_models_box(tmp_path: Path) -> None:
    data, box = make_table_pdf(HEADER, RUBRIC, ruled=False)
    page = page_of(tmp_path, data)
    assert page_tables(page, None).tables == []  # "lines" alone can't see it
    [table] = page_tables(page, [seen(box, 4, 3, "borderless")]).tables
    assert table.header == tuple(HEADER) and table.rows[1] == tuple(RUBRIC[1])


def test_a_grid_that_does_not_match_the_model_stays_prose(tmp_path: Path) -> None:
    data, box = make_table_pdf(HEADER, RUBRIC, ruled=False)
    result = page_tables(page_of(tmp_path, data), [seen(box, 4, 6, "borderless")])
    assert result.tables == [] and result.rejected  # 6 columns: no gutters for them, so no grid


def test_a_picture_of_a_table_uses_the_transcript(tmp_path: Path) -> None:
    data, _ = make_table_pdf(["A"], [], before=INTRO)  # nothing at the box below: no text to read
    transcript = (("Day", "Task"), ("Mon", "Plan sprint"), ("Tue", "Review"))
    box = (60.0, 400.0, 500.0, 500.0)
    [table] = page_tables(page_of(tmp_path, data), [seen(box, 3, 2, "image", transcript=transcript)]).tables
    assert table.source == "vision" and table.header == ("Day", "Task") and len(table.rows) == 2
    empty = (("Day", "Task"), ("", ""), ("", ""))
    result = page_tables(page_of(tmp_path, data), [seen(box, 3, 2, "image", transcript=empty)])
    assert result.tables == [] and result.rejected  # the model couldn't read it either


def test_a_header_over_two_lines_is_folded() -> None:
    header, rows = _header_rows(
        ("Layer", "Complexity", "Sequential"), [("", "", "Operations"), ("Recurrent", "O(n)", "O(n)")]
    )
    assert header == ("Layer", "Complexity", "Sequential Operations") and rows == [
        ("Recurrent", "O(n)", "O(n)")
    ]
    _, rows = _header_rows(("Criterion", "Band 4"), [("Analysis", "Clear")])
    assert rows == [("Analysis", "Clear")]  # a full header and a full row: data, not header


def test_a_table_continued_on_the_next_page_is_joined() -> None:
    first = Table((0, 600, 500, 780), ("A", "B"), (("1", "2"),), "Table 1", continues=True)
    second = Table((0, 60, 500, 120), ("A", "B"), (("3", "4"),))
    [[joined], []] = _join_continued([[first], [second]])
    assert joined.rows == (("1", "2"), ("3", "4")) and joined.last_page == 2


def test_a_table_over_three_pages_is_one_table() -> None:
    first = Table((0, 600, 500, 780), ("A", "B"), (("1", "2"),), "Table 1", continues=True)
    middle = Table((0, 60, 500, 780), ("A", "B"), (("3", "4"),), continues=True)
    last = Table((0, 60, 500, 120), None, (("5", "6"),))
    after = Table((0, 400, 500, 500), ("X", "Y"), (("7", "8"),))
    [[joined], [], [other]] = _join_continued([[first], [middle], [last, after]])
    assert joined.rows == (("1", "2"), ("3", "4"), ("5", "6")) and joined.last_page == 3
    assert not joined.continues and other is after


# ---- parser


def test_table_text_leaves_the_prose(tmp_path: Path) -> None:
    data, box = make_table_pdf(
        HEADER, RUBRIC, caption="Table 1: Essay rubric", before=INTRO, after="Marks are scaled to 40%."
    )
    path = tmp_path / "t.pdf"
    path.write_bytes(data)
    doc = read_pages(path, [PageVision([seen(box, 4, 3, caption="Table 1: Essay rubric")])])
    prose = " ".join(b.text for b in doc.pages[0])
    assert "Analysis" not in prose and "Table 1" not in prose
    assert "rubric below" in prose and "scaled to 40%" in prose
    [table] = doc.tables[0]
    assert table.caption == "Table 1: Essay rubric"


def test_a_page_holding_only_a_table_has_text(tmp_path: Path) -> None:
    data, _ = make_table_pdf(HEADER, RUBRIC)
    path = tmp_path / "t.pdf"
    path.write_bytes(data)
    doc = read_pages(path)  # would be NO_TEXT_LAYER if table text didn't count
    assert doc.pages == [[]] and len(doc.tables[0]) == 1


def test_cells_lose_their_ligatures() -> None:
    assert clean_cell("eﬃcient\nﬁne-tuning") == "efficient fine-tuning"
    assert unligature("ﬀ ﬄ ﬆ") == "ff ffl st"
    assert unligature("10²⁰") == "10²⁰"  # superscripts are left alone (not NFKC)


# ---- outline


def test_a_table_sits_under_its_heading_and_borrows_a_caption() -> None:
    pages = [
        [
            Block("Marking", 18, True, 50, 70),
            Block(INTRO, 10, False, 80, 110),
            Block("Marks are scaled to 40%.", 10, False, 300, 312),
        ]
    ]
    table = Table((60, 150, 530, 230), tuple(HEADER), tuple(tuple(r) for r in RUBRIC))
    paras = outline(pages, [[table]])
    assert [p.table is not None for p in paras] == [False, False, True, False]
    assert paras[2].path == ("Marking",) and paras[2].level is None
    assert paras[2].table is not None
    assert paras[2].table.caption == "The rubric below shows what each band requires."


# ---- chunker


def rubric_para(rows: list[list[str]] | None = None, **kw: Any) -> Para:
    table = Table(
        (0, 0, 1, 1), tuple(HEADER), tuple(tuple(r) for r in rows or RUBRIC), "Table 1: Essay rubric", **kw
    )
    return Para(markdown(table), 2, ("Marking",), None, table)


def test_a_table_is_a_chunk_of_its_own() -> None:
    paras = [Para(INTRO, 1, ("Marking",)), rubric_para(), Para("Marks are scaled to 40%.", 2, ("Marking",))]
    text1, table, text2 = chunk_paras(paras)
    assert (text1.kind, table.kind, text2.kind) == ("text", "table", "text")
    assert [c.ordinal for c in (text1, table, text2)] == [0, 1, 2]
    assert table.content.startswith(
        "Table 1: Essay rubric\n\n| Criterion | Band 4 | Band 2 |\n|---|---|---|\n"
    )
    assert table.section_path == "Marking" and table.page_start == 2
    assert "Criterion: Evidence; Band 4: Cites three sources; Band 2: Cites one source" in (
        table.embed_body or ""
    )
    assert embed_text("Essays", table) == f"Essays\nMarking\n\n{table.embed_body}"


def test_a_big_table_splits_with_the_header_repeated() -> None:
    rows = [
        [f"Criterion {i}", "A long description of what this band requires " * 3, "Less"] for i in range(60)
    ]
    chunks = chunk_paras([rubric_para(rows)])
    assert len(chunks) > 1 and all(c.kind == "table" for c in chunks)
    assert all(c.token_count <= MAX_TOKENS for c in chunks)
    assert all("| Criterion | Band 4 | Band 2 |" in c.content for c in chunks)
    assert CONTINUED in chunks[1].content and CONTINUED not in chunks[0].content
    assert sum(c.content.count("| Criterion ") for c in chunks) == 60 + len(
        chunks
    )  # every row once, + headers
    assert all(len(encoding().encode(c.content)) == c.token_count for c in chunks)


def test_a_transcribed_table_says_so() -> None:
    [chunk] = chunk_paras([rubric_para(source="vision")])
    assert chunk.source == "vision" and TRANSCRIBED in chunk.content.split("\n")[0]


# ---- verified layouts for tables no method could read

SPARSE_HEADER = ["Model", "h", "dk", "BLEU"]
SPARSE = [["base", "8", "64", "25.8"], ["(A)", "", "16", "24.9"], ["(B)", "4", "", "25.5"]]


def box_words(tmp_path: Path, header: list[str], rows: list[list[str]]) -> tuple[list, tuple]:
    from ingest.geometry import center_in, pad
    from ingest.pages import visible_words

    data, box = make_table_pdf(header, rows, ruled=False)
    page = page_of(tmp_path, data)
    return [w for w in visible_words(page) if center_in(w, pad(box))], box


def test_a_layout_made_of_the_boxs_words_in_place_is_accepted(tmp_path: Path) -> None:
    from ingest.tables import verify_layout

    words, box = box_words(tmp_path, SPARSE_HEADER, SPARSE)
    rows = tuple(tuple(r) for r in SPARSE)
    assert verify_layout(tuple(SPARSE_HEADER), rows, words, seen(box, 4, 4, "borderless")) is None


def test_a_layout_with_an_invented_or_missing_word_is_rejected(tmp_path: Path) -> None:
    from ingest.tables import verify_layout

    words, box = box_words(tmp_path, SPARSE_HEADER, SPARSE)
    invented = tuple(tuple(r) for r in [*SPARSE[:2], ["(B)", "4", "", "27.0"]])
    assert "not in the box" in (
        verify_layout(tuple(SPARSE_HEADER), invented, words, seen(box, 4, 4, "borderless")) or ""
    )
    dropped = tuple(tuple(r) for r in SPARSE[:2])  # the (B) row left out
    assert "leaves out" in (
        verify_layout(tuple(SPARSE_HEADER), dropped, words, seen(box, 4, 4, "borderless")) or ""
    )


def test_a_value_moved_to_the_wrong_column_is_rejected(tmp_path: Path) -> None:
    from ingest.tables import verify_layout

    words, box = box_words(tmp_path, SPARSE_HEADER, SPARSE)
    shifted = tuple(tuple(r) for r in [SPARSE[0], ["(A)", "16", "", "24.9"], SPARSE[2]])  # 16 is dk, not h
    assert "another column" in (
        verify_layout(tuple(SPARSE_HEADER), shifted, words, seen(box, 4, 4, "borderless")) or ""
    )


def test_a_rejected_table_is_laid_out_once_and_read_as_pdf_text(
    tmp_path: Path, openai_fake: FakeOpenAI
) -> None:
    import asyncio
    import json

    from test_vision import chat_reply

    from ingest import rescue

    data, box = make_table_pdf(SPARSE_HEADER, SPARSE, ruled=False)
    path = tmp_path / "t.pdf"
    path.write_bytes(data)
    bad = seen(box, 3, 7, "borderless")  # the page call's column count is off: no method can read the table
    assert page_tables(page_of(tmp_path, data), [bad]).tables == []
    openai_fake.reply(json=chat_reply(json.dumps({"header": SPARSE_HEADER, "rows": SPARSE})))
    cache = tmp_path / "cache"
    [page] = asyncio.run(
        rescue.rescue_tables(path, [PageVision([bad])], model="figure-model", cache_dir=cache)
    )
    assert page is not None
    [table] = page_tables(page_of(tmp_path, data), page.tables).tables
    assert (
        table.source == "pdf"
        and table.header == tuple(SPARSE_HEADER)
        and table.rows[1] == ("(A)", "", "16", "24.9")
    )
    asyncio.run(rescue.rescue_tables(path, [PageVision([bad])], model="figure-model", cache_dir=cache))
    assert len(openai_fake.requests) == 1  # cached
