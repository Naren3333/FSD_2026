"""pdf parsing, header/footer stripping and the ingest failure reasons."""

from pathlib import Path

import pymupdf
import pytest
from conftest import make_mixed_pdf, make_pdf, make_structured_pdf

from ingest.parse import (
    Block,
    IngestError,
    Pages,
    _blocks,
    _columns,
    _without_regions,
    read_pages,
    reading_key,
)
from ingest.tables import Table


def write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(data)
    return path


def failure(path: Path) -> str:
    with pytest.raises(IngestError) as e:
        read_pages(path)
    return e.value.reason


def prose(path: Path) -> Pages:
    return read_pages(path).pages


def texts(pages: Pages) -> list[list[str]]:
    return [[b.text for b in blocks] for blocks in pages]


def test_text_blocks_per_page(tmp_path: Path) -> None:
    paras = [
        ["First paragraph about the light-dependent reactions.", "Second paragraph about the Calvin cycle."],
        ["Third paragraph, on the next page, about stomata and gas exchange."],
    ]
    assert texts(prose(write(tmp_path, make_pdf(paragraphs=paras)))) == paras


def test_mostly_text_is_fine(tmp_path: Path) -> None:
    pages = prose(write(tmp_path, make_mixed_pdf("tit")))
    assert len(pages) == 3 and pages[1] == []


@pytest.mark.parametrize("kinds", ["i", "iit", "titii"])
def test_mostly_images_is_no_text_layer(tmp_path: Path, kinds: str) -> None:
    assert failure(write(tmp_path, make_mixed_pdf(kinds))) == "NO_TEXT_LAYER"


def test_short_text_pages_count_as_empty(tmp_path: Path) -> None:
    assert failure(write(tmp_path, make_pdf(pages=3, text_="Hi"))) == "NO_TEXT_LAYER"


def test_password_pdf_is_encrypted(tmp_path: Path) -> None:
    assert failure(write(tmp_path, make_pdf(password="secret"))) == "ENCRYPTED"


def test_owner_password_only_is_readable(tmp_path: Path) -> None:
    doc = pymupdf.open(stream=make_pdf(), filetype="pdf")
    data = doc.tobytes(encryption=pymupdf.mupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="")
    assert prose(write(tmp_path, data))[0][0].text.startswith("Photosynthesis")


def test_garbage_after_the_magic_is_corrupt(tmp_path: Path) -> None:
    assert failure(write(tmp_path, b"%PDF-1.7\n" + bytes(range(256)) * 16)) == "CORRUPT"


def test_missing_file_is_not_a_failure_reason(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_pages(tmp_path / "gone.pdf")


@pytest.mark.parametrize(
    ("error", "corrupt"),
    [(RuntimeError("MuPDF error: format error"), True), (KeyError("bbox"), False), (RecursionError(), False)],
)
def test_only_pymupdf_errors_mean_a_corrupt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, corrupt: bool
) -> None:
    """a bug here, or a model answer it trips over, is a 500 the caller retries, not a permanent corrupt."""
    import ingest.parse

    def broken(*args: object) -> list[Table]:
        raise error

    monkeypatch.setattr(ingest.parse, "_page_tables", broken)
    path = write(tmp_path, make_pdf())
    if corrupt:
        assert failure(path) == "CORRUPT"
    else:
        with pytest.raises(type(error)):
            read_pages(path)


PARA = "Leaves are the main site of photosynthesis in most plants and hold many chloroplasts. " * 2


def paged(pages: int) -> list[tuple[str, str]]:
    """one heading + paragraphs per page."""
    return [e for n in range(pages) for e in [("page", ""), ("h1", f"Chapter {n + 1}"), *[("p", PARA)] * 3]]


def test_blocks_carry_size_and_weight(tmp_path: Path) -> None:
    pages = prose(write(tmp_path, make_structured_pdf([("h1", "Plants"), ("h3", "Leaves"), ("p", PARA)])))
    assert [(b.text, b.size, b.bold) for b in pages[0]] == [
        ("Plants", 18, True),
        ("Leaves", 10, True),
        (PARA.strip(), 10, False),
    ]


def test_a_heading_line_inside_a_paragraph_block_is_its_own_block(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    # one insert_htmlbox → one pymupdf block holding a bold line, then plain lines.
    page.insert_htmlbox(pymupdf.Rect(50, 50, 545, 300), f"<b>Gas exchange</b><br>{PARA}")
    data = doc.tobytes()
    blocks = prose(write(tmp_path, data))[0]
    assert [(b.text, b.bold) for b in blocks] == [("Gas exchange", True), (PARA.strip(), False)]


def test_repeated_header_and_footer_are_dropped(tmp_path: Path) -> None:
    data = make_structured_pdf(paged(4), header="Biology Notes", footer="Year 9 Biology - page # of 4")
    pages = texts(prose(write(tmp_path, data)))
    assert len(pages) == 4
    assert pages == [[f"Chapter {n + 1}", *[PARA.strip()] * 3] for n in range(4)]


def test_short_documents_keep_everything(tmp_path: Path) -> None:
    data = make_structured_pdf(paged(2), footer="Year 9 Biology - page #")
    pages = texts(prose(write(tmp_path, data)))
    assert [f"Year 9 Biology - page {n}" in p for n, p in enumerate(pages, start=1)] == [True, True]


def test_repeated_body_text_is_kept(tmp_path: Path) -> None:
    # "key idea" sits mid-page on every page, so it is not a header or footer.
    elements = [
        e
        for n in range(4)
        for e in [("page", ""), ("h1", f"Chapter {n + 1}"), ("p", PARA), ("p", "Key idea"), ("p", PARA)]
    ]
    pages = texts(prose(write(tmp_path, make_structured_pdf(elements))))
    assert len(pages) == 4 and all("Key idea" in p for p in pages)


def test_a_footer_above_the_bottom_tenth_is_still_dropped(tmp_path: Path) -> None:
    # like rfc 9110: an a4 page whose footer sits ~87% of the way down.
    doc = pymupdf.open()
    for n in range(1, 5):
        page = doc.new_page(width=595, height=841)
        page.insert_textbox(pymupdf.Rect(72, 100, 523, 400), f"Chapter {n} body. {PARA}", fontsize=10)
        page.insert_text((72, 733), f"Fielding, et al. Standards Track Page {n}", fontsize=9)
    pages = texts(prose(write(tmp_path, doc.tobytes())))
    assert not any("Standards Track" in t for page in pages for t in page)


def test_hyphenated_line_breaks_are_joined(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_htmlbox(
        pymupdf.Rect(50, 50, 545, 300),
        "the chunks are turned into vector repre-<br>sentations by a well-<br>Known model",
    )
    [block] = prose(write(tmp_path, doc.tobytes()))[0]
    assert block.text == "the chunks are turned into vector representations by a well- Known model"


def _span(text: str, x0: float, x1: float, y0: float = 100, bold: bool = False) -> dict:
    return {
        "text": text,
        "bbox": (x0, y0, x1, y0 + 12),
        "size": 10.0,
        "flags": 16 if bold else 0,
        "font": "Serif-Bold" if bold else "Serif",
    }


def _block(*lines: list[dict]) -> dict:
    boxes = [s["bbox"] for line in lines for s in line]
    return {
        "type": 0,
        "bbox": (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        ),
        "lines": [
            {
                "spans": line,
                "bbox": (line[0]["bbox"][0], line[0]["bbox"][1], line[-1]["bbox"][2], line[-1]["bbox"][3]),
            }
            for line in lines
        ],
    }


def test_inline_words_typeset_as_their_own_block_go_back_into_their_line() -> None:
    # like rfc 9110: "may" is its own block, sitting in the gap of a line of the paragraph's block.
    para = _block(
        [_span("For historical reasons, a user agent ", 72, 300), _span(" change the method", 325, 500)],
        [_span("from POST to GET for the next request.", 72, 400, y0=114)],
    )
    may = _block([_span("MAY", 302, 323, bold=True)])
    opening = _block([_span("SHOULD", 30, 70, y0=114, bold=True)])  # opens the second line, just left of it
    blocks = _blocks({"blocks": [para, may, opening]})
    assert [b.text for b in blocks] == [
        "For historical reasons, a user agent MAY change the method SHOULD from POST to GET for the next request."
    ]


def test_a_short_block_on_its_own_line_stays_a_block() -> None:
    heading = _block([_span("Summary", 72, 130, y0=60, bold=True)])
    para = _block([_span("The body text of the section.", 72, 300)])
    assert [b.text for b in _blocks({"blocks": [heading, para]})] == [
        "Summary",
        "The body text of the section.",
    ]


# ---- ligatures

LIGATURE_TEXT = (
    "The data eﬃciency of zero-shot transfer varies widely, and ﬁne-tuning on the ﬂow of"
    " labelled examples changes it."
)


def ligature_page(tmp_path: Path, html: str) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_htmlbox(pymupdf.Rect(72, 72, 520, 760), html)
    path = tmp_path / "lig.pdf"
    doc.save(path)
    return path


def test_ligatures_come_out_as_plain_letters(tmp_path: Path) -> None:
    path = ligature_page(tmp_path, f"<p>{LIGATURE_TEXT}</p>")
    assert "ﬃ" in pymupdf.open(path)[0].get_text()  # the text layer really holds the ligature
    [[block]] = prose(path)
    assert "efficiency" in block.text and "fine-tuning" in block.text and "flow" in block.text


def test_a_caption_line_with_a_ligature_is_stripped(tmp_path: Path) -> None:
    path = ligature_page(tmp_path, f"<p>{LIGATURE_TEXT}</p><p>Table 1: The eﬃciency of each method</p>")
    page = pymupdf.open(path)[0]
    caption = next(
        ln
        for b in page.get_text("dict")["blocks"]
        if b["type"] == 0
        for ln in b["lines"]
        if "Table 1" in "".join(s["text"] for s in ln["spans"])
    )
    below = caption["bbox"][3] + 10
    table = Table(
        (72.0, below, 520.0, below + 60), ("A", "B"), (("1", "2"),), "Table 1: The efficiency of each method"
    )
    kept = _blocks(_without_regions(page.get_text("dict"), [table]))
    assert [b.text for b in kept] == [LIGATURE_TEXT.replace("ﬃ", "ffi").replace("ﬁ", "fi").replace("ﬂ", "fl")]


def test_a_caption_line_is_stripped_when_the_model_changes_its_punctuation(tmp_path: Path) -> None:
    path = ligature_page(tmp_path, f"<p>{LIGATURE_TEXT}</p><p>Figure 7. The data efficiency of zero-shot</p>")
    page = pymupdf.open(path)[0]
    caption = next(
        ln
        for b in page.get_text("dict")["blocks"]
        if b["type"] == 0
        for ln in b["lines"]
        if "Figure 7" in "".join(s["text"] for s in ln["spans"])
    )
    above = caption["bbox"][1] - 10
    figure = Table(
        (72.0, above - 60, 520.0, above), ("A",), (("1",),), "Figure 7: The data efficiency of zero-shot"
    )
    kept = _blocks(_without_regions(page.get_text("dict"), [figure]))
    assert not any("Figure 7" in b.text for b in kept)


# ---- two columns

COLUMN_TEXT = "Attention lets every position look at every other position in one step, so paths stay short."


def column_page() -> list[Block]:
    """a spanning title, then two columns of four 3-line paragraphs each, interleaved as some pdfs store them."""
    left = [Block(f"L{n} {COLUMN_TEXT}", 10, False, 100 + 50 * n, 136 + 50 * n, 50, 290) for n in range(4)]
    right = [Block(f"R{n} {COLUMN_TEXT}", 10, False, 100 + 50 * n, 136 + 50 * n, 310, 550) for n in range(4)]
    return [Block("Model architecture", 14, True, 60, 76, 50, 550)] + [
        b for pair in zip(left, right, strict=False) for b in pair
    ]


def test_a_two_column_page_is_read_column_by_column() -> None:
    blocks = column_page()
    cols = _columns(blocks, 600, 10)
    assert cols is not None and 290 <= cols.gutter <= 310 and cols.cuts == (60,)
    ordered = sorted(blocks, key=lambda b: reading_key(cols, (b.x0, b.top, b.x1, b.bottom)))
    assert [b.text[:2] for b in ordered] == ["Mo", "L0", "L1", "L2", "L3", "R0", "R1", "R2", "R3"]


def test_slides_and_short_side_text_are_single_column() -> None:
    slide = [Block("Why we test", 32, True, 40, 80, 40, 700)] + [
        Block(f"▪ Point {n} {COLUMN_TEXT}", 24, False, 120 + 60 * n, 170 + 60 * n, 40, 700) for n in range(4)
    ]
    assert _columns(slide, 960, 24) is None
    two_boxes = column_page()[:5]  # two paragraphs a side: not enough for columns
    assert _columns(two_boxes, 600, 10) is None


def test_a_table_in_the_right_column_comes_after_the_left_column() -> None:
    from ingest.headings import outline

    blocks = column_page()
    cols = _columns(blocks, 600, 10)
    ordered = sorted(blocks, key=lambda b: reading_key(cols, (b.x0, b.top, b.x1, b.bottom)))
    table = Table((310.0, 170.0, 550.0, 190.0), ("A", "B"), (("1", "2"),), "Table 1: Results")
    texts = [p.text[:2] if p.table is None else "T" for p in outline([ordered], [[table]], columns=[cols])]
    assert texts == ["Mo", "L0", "L1", "L2", "L3", "R0", "R1", "T", "R2", "R3"]
    by_height = [p.text[:2] if p.table is None else "T" for p in outline([blocks], [[table]])]
    assert by_height.index("L1") < by_height.index("T") < by_height.index("L2")  # mid left column


def test_a_pdf_in_two_columns_is_parsed_in_reading_order(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    paras = "".join(f"<p>{{side}}{n} {COLUMN_TEXT} {COLUMN_TEXT}</p>" for n in range(4))
    page.insert_htmlbox(pymupdf.Rect(310, 100, 550, 760), paras.replace("{side}", "Right"))  # stored first
    page.insert_htmlbox(pymupdf.Rect(50, 100, 290, 760), paras.replace("{side}", "Left"))
    path = tmp_path / "cols.pdf"
    doc.save(path)
    parsed = read_pages(path)
    assert parsed.columns[0] is not None
    assert [b.text.split()[0] for b in parsed.pages[0]] == [f"Left{n}" for n in range(4)] + [
        f"Right{n}" for n in range(4)
    ]
