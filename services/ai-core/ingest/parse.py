"""PDF → styled text blocks and tables per page with PyMuPDF. Blocking: run it
in a thread.

Tables come from ingest.tables (inside the vision pass's boxes, or find_tables "lines" without one); their
text, and their caption line, are taken out of the prose so cells never turn into headings or paragraphs. A
table continued on the next page (the model says so, and the next page opens with a table of the same width)
is joined into one. Described figures (ingest.figures) take their words and caption line out of the prose
the same way, unless they cover most of the page. Repeated headers and footers are dropped here; headings are
detected from the blocks' styles in ingest.headings.

A page without a text layer that the vision pass transcribed (OCR) takes its text from
the transcript instead: headings become blocks sized above the document's body text (by level), paragraphs
body-size blocks, and tables vision tables, laid out top to bottom in the page's middle band (so they are
never mistaken for headers or footers). Transcribed pages are listed in `Document.ocr_pages`.

A page set in two columns has a gutter: a vertical strip in the middle of the page that no
column's text crosses (`_columns`). Its blocks are put in reading order (the left column, then the right, band by
band between blocks spanning both), and `reading_key` places tables and figures by the same order; a page without
a gutter keeps the PDF's own order and is placed top to bottom.
"""

import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ingest import tables as table_extraction
from ingest.figures import Figure, page_figures
from ingest.pages import MIN_PAGE_CHARS
from ingest.strings import join_lines, unligature
from ingest.tables import Table
from ingest.vision import PageVision, SeenText

MAX_EMPTY_SHARE = 0.5  # more empty pages than this → NO_TEXT_LAYER
REPEAT_SHARE = 0.6  # a header/footer appears on more than this share of pages...
MARGIN_BAND = 0.15  # ...within this share of the page height from the top or bottom edge...
MIN_REPEAT_PAGES = 3  # ...in a document with at least this many pages (else every block "repeats"),
# and is short and no bigger than the body text (so "Chapter 1", "Chapter 2"… at the top of each page,
# or each page's first paragraph, stay)
MAX_REPEAT_CHARS = 120
BOLD_FLAG = 16
MAX_INLINE_CHARS = 12  # a one-span block this short, inside another block's line, is an inline word
TABLE_PAD = 2.0  # a text line whose centre is this close inside a table's box is the table's
CAPTION_REACH = 40.0  # a table's caption line sits within this distance above or below it
CAPTION_WORDS = 3  # ...and starts with the caption's first words
OCR_BODY_SIZE = 12.0  # a transcribed page's body text, when the document has no text layer to take it from
OCR_HEADING_RATIOS = {1: 2.0, 2: 1.6, 3: 1.3}  # a transcribed heading's size, by level, over the body size
OCR_BAND = (0.2, 0.8)  # transcribed blocks are laid out within this share of the page height
GUTTER_RANGE = (0.3, 0.7)  # a column gutter lies within this share of the page width...
GUTTER_MIN_BLOCKS = 3  # ...with at least this many body-size blocks on each side...
GUTTER_MIN_LINES = 5  # ...holding at least this many lines...
GUTTER_MIN_SHARE = (
    0.5  # ...and the two sides' text running over at least this share of the page's text height
)
SPAN_SHARE = (
    0.55  # a block wider than this share of the text width spans both columns (a title, a wide figure)
)


@dataclass(frozen=True)
class Block:
    """A run of same-style lines from one PyMuPDF text block, whitespace-normalised."""

    text: str
    size: float  # of the line's longest span, rounded to 0.5pt
    bold: bool
    top: float
    bottom: float
    x0: float = 0.0
    x1: float = 0.0


Pages = list[list[Block]]  # pages[i] = the text blocks of page i + 1


@dataclass(frozen=True)
class Document:
    pages: Pages  # the prose: text blocks per page, table text taken out
    tables: list[list[Table]]  # tables[i] = the tables of page i + 1, top to bottom
    figures: list[list[Figure]] = field(default_factory=list)  # figures[i] = page i + 1's described figures
    ocr_pages: frozenset[int] = (
        frozenset()
    )  # 1-based: pages with text the vision model transcribed (all or part)
    toc: list[tuple[int, str, int]] = field(
        default_factory=list
    )  # the PDF outline: (level, title, 1-based page)
    columns: list["Columns | None"] = field(
        default_factory=list
    )  # columns[i]: page i + 1's gutter, if two-column


@dataclass(frozen=True)
class Columns:
    """A two-column page: the gutter's x, and the tops of the blocks spanning both columns (band boundaries)."""

    gutter: float
    cuts: tuple[float, ...] = ()


class IngestError(Exception):
    """A failure with a `failure_reason` of its own (anything else becomes INTERNAL)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@contextmanager
def open_checked(path: Path) -> Iterator[pymupdf.Document]:
    """The PDF, opened; ENCRYPTED or CORRUPT when it can't be read. Ingest also runs this before the vision
    pass, so a bad file fails with its own reason and costs no vision calls."""
    if not path.exists():
        # deleted mid-ingest: INTERNAL, if the row survives
        raise FileNotFoundError(f"{path.name} is missing")
    problem: str | None = None
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        problem = str(e)
    # Raised outside the except block: the failed open's traceback would otherwise keep the file open, and
    # Windows then cannot delete the temporary copy.
    if problem is not None:
        raise IngestError("CORRUPT", f"PyMuPDF could not open the file: {problem}")
    with doc:
        if doc.needs_pass:
            raise IngestError("ENCRYPTED", "The PDF needs a password")
        yield doc


def read_pages(path: Path, vision: list[PageVision | None] | None = None) -> Document:
    """The document's prose, tables and figures. `vision` is the vision pass per page (None, or None for a
    page: that page falls back to find_tables "lines" and has no figures)."""
    with open_checked(path) as doc:
        try:
            doc_pages = list(doc.pages())
            seen = [
                vision[page.number] if vision and page.number < len(vision) else None for page in doc_pages
            ]
            ocr = [s is not None and s.transcript is not None for s in seen]
            tables = [
                [] if o else _page_tables(page, s) for page, s, o in zip(doc_pages, seen, ocr, strict=False)
            ]
            figures = [
                page_figures(page, s.figures, found) if s else []
                for page, s, found in zip(doc_pages, seen, tables, strict=False)
            ]
            pages = [
                [] if o else _blocks(_without_regions(page.get_text("dict"), found, figs))
                for page, s, found, figs, o in zip(doc_pages, seen, tables, figures, ocr, strict=False)
            ]
            heights = [page.rect.height for page in doc_pages]
            widths = [page.rect.width for page in doc_pages]
            toc = [(e[0], unligature(" ".join(str(e[1]).split())), e[2]) for e in doc.get_toc()]
        except Exception as e:
            raise IngestError("CORRUPT", f"PyMuPDF could not read the text: {e}") from e
    body = body_size(pages) or OCR_BODY_SIZE
    columns = [None if ocr[n] else _columns(pages[n], widths[n], body) for n in range(len(pages))]
    pages = [
        sorted(blocks, key=lambda b: reading_key(cols, (b.x0, b.top, b.x1, b.bottom))) if cols else blocks
        for blocks, cols in zip(pages, columns, strict=False)
    ]
    for n, s in enumerate(seen):
        if ocr[n]:
            assert s is not None and s.transcript is not None
            band = (0.0, heights[n] * OCR_BAND[0], widths[n], heights[n] * OCR_BAND[1])
            pages[n], tables[n] = _laid_out(s.transcript, body, band)
    tables = [
        sorted(found, key=lambda t: reading_key(cols, t.bbox))
        for found, cols in zip(tables, columns, strict=False)
    ]
    transcribed = frozenset(n + 1 for n, o in enumerate(ocr) if o)
    chars = [
        len("".join(b.text for b in blocks))
        + sum(_table_chars(t) for t in found)
        + sum(len(f.words) for f in figs if f.strip)
        for blocks, found, figs in zip(pages, tables, figures, strict=False)
    ]
    empty = sum(1 for n in chars if n < MIN_PAGE_CHARS)
    if not pages or empty / len(pages) > MAX_EMPTY_SHARE:
        raise IngestError(
            "NO_TEXT_LAYER", f"{empty} of {len(pages)} pages have under {MIN_PAGE_CHARS} characters of text"
        )
    figures = [
        sorted(figs, key=lambda f: reading_key(cols, f.bbox))
        for figs, cols in zip(figures, columns, strict=False)
    ]
    return Document(
        _strip_repeated(pages, heights), _join_continued(tables), figures, transcribed, toc, columns
    )


def reading_key(columns: "Columns | None", box: tuple[float, float, float, float]) -> tuple[int, int, float]:
    """Where a box comes in the page's reading order: (band, column, top). Without columns, top to bottom. On a
    two-column page a band runs from one spanning block to the next; in it the spanning block comes first, then
    the left column, then the right."""
    if columns is None:
        return (0, 0, box[1])
    band = sum(1 for c in columns.cuts if c <= box[1])
    if box[0] < columns.gutter < box[2]:
        return (band, 0, box[1])
    return (band, 1 if (box[0] + box[2]) / 2 < columns.gutter else 2, box[1])


def _columns(blocks: list[Block], width: float, body: float) -> Columns | None:
    """The page's column gutter (see the module docstring), or None for a single-column page."""
    text = [b for b in blocks if b.x1 > b.x0]
    if len(text) < 2 * GUTTER_MIN_BLOCKS:
        return None
    left_edge, right_edge = min(b.x0 for b in text), max(b.x1 for b in text)
    spanning = [b for b in text if b.x1 - b.x0 > SPAN_SHARE * (right_edge - left_edge)]
    narrow = [b for b in text if b not in spanning]
    height = max(b.bottom for b in text) - min(b.top for b in text)
    best: tuple[float, float] | None = None  # (shorter side's height, gutter x)
    for x in range(int(width * GUTTER_RANGE[0]), int(width * GUTTER_RANGE[1]) + 1):
        if any(b.x0 < x < b.x1 for b in narrow):
            continue
        sides = [[b for b in narrow if b.x1 <= x], [b for b in narrow if b.x0 >= x]]
        if not all(_column_like(side, body) for side in sides):
            continue
        short = min(sum(b.bottom - b.top for b in side) for side in sides)
        if short >= GUTTER_MIN_SHARE * height and (best is None or short > best[0]):
            best = (short, float(x))
    if best is None:
        return None
    gutter = best[1]
    return Columns(gutter, tuple(sorted(b.top for b in spanning if b.x0 < gutter < b.x1)))


def _column_like(side: list[Block], body: float) -> bool:
    """Enough body text on one side of a gutter to be a column."""
    prose = [b for b in side if abs(b.size - body) <= 1]
    lines = sum(max(1, round((b.bottom - b.top) / (b.size * 1.2))) for b in prose if b.size)
    return len(prose) >= GUTTER_MIN_BLOCKS and lines >= GUTTER_MIN_LINES


def _laid_out(
    transcript: tuple[SeenText, ...], body: float, box: tuple[float, float, float, float]
) -> tuple[list[Block], list[Table]]:
    """A transcript as blocks and tables, each given an equal slot down `box` (the page's middle band)."""
    blocks: list[Block] = []
    tables: list[Table] = []
    x0, top, x1, bottom = box
    step = (bottom - top) / max(1, len(transcript))
    for i, t in enumerate(transcript):
        y0, y1 = top + i * step, top + (i + 0.8) * step
        if t.kind == "table":
            tables.append(Table((x0, y0, x1, y1), t.header, t.rows, None, "vision"))
        else:
            heading = t.kind == "heading"
            size = round(body * OCR_HEADING_RATIOS[t.level] * 2) / 2 if heading else body
            blocks.append(Block(t.text, size, heading, y0, y1, x0, x1))
    return blocks, tables


def _page_tables(page: pymupdf.Page, seen: PageVision | None) -> list[Table]:
    return sorted(
        table_extraction.page_tables(page, seen.tables if seen else None).tables, key=lambda t: t.bbox[1]
    )


def _caption_key(text: str) -> str:
    """Letters and digits only, lower case: the model writes "Figure 7:" where the PDF has "Figure 7.", and
    some text layers run the words together ("Thedataefficiencyof")."""
    return re.sub(r"[^0-9a-z]", "", unligature(text).lower())


def _without_regions(page_dict: dict, tables: list[Table], figures: list[Figure] | None = None) -> dict:
    """The page's text minus the lines inside a table or a (stripped) figure, and minus the tables' and figures'
    caption lines."""
    regions: list[Table | Figure] = [*tables, *(figures or [])]
    if not regions:
        return page_dict
    boxes = [
        (r.bbox[0] - TABLE_PAD, r.bbox[1] - TABLE_PAD, r.bbox[2] + TABLE_PAD, r.bbox[3] + TABLE_PAD)
        for r in regions
        if getattr(r, "strip", True)
    ]
    captions = [
        (r, key)
        for r in regions
        if r.caption
        if (key := _caption_key(" ".join(r.caption.split()[:CAPTION_WORDS])))
    ]

    def keep(line: dict) -> bool:
        x0, y0, x1, y1 = line["bbox"]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if any(b[0] <= mx <= b[2] and b[1] <= my <= b[3] for b in boxes):
            return False
        text = _caption_key("".join(s["text"] for s in line["spans"]))
        return not any(
            text.startswith(key) and min(abs(y0 - t.bbox[3]), abs(t.bbox[1] - y1)) <= CAPTION_REACH
            for t, key in captions
        )

    blocks = []
    for b in page_dict["blocks"]:
        if b["type"] != 0:
            blocks.append(b)
            continue
        lines = [ln for ln in b["lines"] if keep(ln)]
        if lines:
            blocks.append({**b, "lines": lines})
    return {**page_dict, "blocks": blocks}


def _table_chars(t: Table) -> int:
    return sum(len(c) for row in (t.header or (), *t.rows) for c in row)


def _join_continued(tables: list[list[Table]]) -> list[list[Table]]:
    """A table the model says goes on, followed by a table of the same width at the top of the next page,
    is one table: the second's rows are appended (its header too, unless it repeats the first's). The joined
    table stays open while it goes on, so a table over three or more pages ends up as one."""
    out = [list(page) for page in tables]
    open_at: tuple[int, int] | None = None  # (page index, table index) of a table going on to the next page
    for i, page in enumerate(out):
        if open_at is not None and page:
            j, k = open_at
            first, second = out[j][k], page[0]
            if table_extraction.width(first) == table_extraction.width(second):
                extra = () if second.header in (None, first.header) else (second.header,)
                out[j][k] = Table(
                    first.bbox,
                    first.header,
                    (*first.rows, *extra, *second.rows),
                    first.caption,
                    first.source,
                    second.continues,
                    second.last_page or i + 1,
                )
                del page[0]
                if not page:
                    open_at = (j, k) if out[j][k].continues else None
                    continue
        open_at = (i, len(page) - 1) if page and page[-1].continues else None
    return out


def _blocks(page_dict: dict) -> list[Block]:
    """Text blocks (type 0) split wherever the line style changes, so a heading line at the top of a
    paragraph's block becomes a block of its own."""
    out: list[Block] = []
    for block in _inline_words([b for b in page_dict["blocks"] if b["type"] == 0]):
        run: list[tuple[str, float, bool, float, float, float, float]] = []
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            main = max(spans, key=lambda s: len(s["text"]))
            style = (round(main["size"] * 2) / 2, bool(main["flags"] & BOLD_FLAG) or "Bold" in main["font"])
            text = unligature(" ".join("".join(s["text"] for s in line["spans"]).split()))
            if run and (run[-1][1], run[-1][2]) != style:
                out.append(_join(run))
                run = []
            run.append((text, *style, line["bbox"][1], line["bbox"][3], line["bbox"][0], line["bbox"][2]))
        if run:
            out.append(_join(run))
    return out


def _inline_words(blocks: list[dict]) -> list[dict]:
    """Put one-word blocks back into the line they sit in. Some PDFs (RFC 9110's MUST / SHOULD / MAY)
    typeset an inline word as a block of its own, which would pull it out of its sentence. A block is
    such a word when it is a single short span whose vertical middle falls inside a line of another
    block, horizontally within or touching that block (a word opening a line sits just left of it)."""
    kept: list[dict] = []
    for word in blocks:
        lines = word["lines"]
        spans = lines[0]["spans"] if len(lines) == 1 else []
        if len(spans) != 1 or not 0 < len(spans[0]["text"].strip()) <= MAX_INLINE_CHARS:
            kept.append(word)
            continue
        x0, y0, x1, y1 = word["bbox"]
        middle = (y0 + y1) / 2
        target = next(
            (
                line
                for block in blocks
                if block is not word
                and block["lines"] is not lines
                and x1 >= block["bbox"][0] - 2
                and x0 <= block["bbox"][2] + 2  # inside or touching
                for line in block["lines"]
                if line["bbox"][1] <= middle <= line["bbox"][3]
            ),
            None,
        )
        if target is None:
            kept.append(word)
            continue
        span = {**spans[0], "text": f" {spans[0]['text'].strip()} "}
        target["spans"] = sorted([*target["spans"], span], key=lambda s: s["bbox"][0])
        word["lines"] = []  # a word moved into a line can't receive others
    return [b for b in kept if b["lines"]]


def _join(run: list[tuple[str, float, bool, float, float, float, float]]) -> Block:
    text = run[0][0]
    for r in run[1:]:
        text = join_lines(text, r[0])
    return Block(
        text, run[0][1], run[0][2], run[0][3], run[-1][4], min(r[5] for r in run), max(r[6] for r in run)
    )


def body_size(pages: Pages) -> float:
    """The median font size, weighted by characters."""
    sizes = sorted((b.size, len(b.text)) for blocks in pages for b in blocks)
    half, seen = sum(n for _, n in sizes) / 2, 0
    for size, n in sizes:
        seen += n
        if seen >= half:
            return size
    return 0.0


def _repeat_key(block: Block, height: float, body: float) -> tuple[str, int] | None:
    band = height * MARGIN_BAND
    mid_page = band < block.top and block.bottom < height - band
    if mid_page or block.size > body or len(block.text) > MAX_REPEAT_CHARS:
        return None  # mid-page, bigger than body text or long: never a header or footer
    return re.sub(r"\d+", "#", block.text.lower()), round(block.top / 3)


def _strip_repeated(pages: Pages, heights: list[float]) -> Pages:
    """Drop headers/footers: the same text (digits ignored, so page numbers match) at the same
    y-position, in the top or bottom margin band, short and no bigger than body text, on more than
    REPEAT_SHARE of the pages."""
    if len(pages) < MIN_REPEAT_PAGES:
        return pages
    body = body_size(pages)
    counts = Counter(
        k
        for blocks, h in zip(pages, heights, strict=False)
        for k in {_repeat_key(b, h, body) for b in blocks}
        if k is not None
    )
    repeated = {k for k, n in counts.items() if n > REPEAT_SHARE * len(pages)}
    return [
        [b for b in blocks if _repeat_key(b, h, body) not in repeated]
        for blocks, h in zip(pages, heights, strict=False)
    ]
