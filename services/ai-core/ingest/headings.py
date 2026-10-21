"""headings and section paths from block styles."""

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from ingest.figures import Figure
from ingest.parse import Block, Columns, Pages, body_size, join_lines, reading_key
from ingest.tables import Table, markdown

MAX_HEADING_CHARS = 150
SIZE_RATIO = 1.15  # a heading is at least this much bigger than the body text...
BOLD_RATIO = 0.95  # ...or bold and at least about body size (unless the body itself is bold)
MAX_LEVELS = 4  # smaller heading sizes merge into the last level
NOT_HEADING_ENDINGS = ".,;"
BULLETS = "▪•◦‣●○■□►▸➢➤✓✔–"  # a block starting with one of these is a list item, never a heading
MIN_TITLE_PAGES = 3  # documents shorter than this keep every heading size (each one is "page 1 only")
CONTINUATION_BREAKS = ".!?:"  # a body line ending in one of these is never continued by the next
PAGE_NUMBER = re.compile(r"\d{1,3}")
FRONT_PAGES = 2  # cover + title page: a heading size used only here is the document's title
# a heading line starting like this is a heading of its own, never the wrapped rest of the one above
NUMBERED = re.compile(r"(\d+\.)+\d*\s|\d+\s|[A-Z]\.\s|(appendix|chapter|part|section)\s", re.IGNORECASE)
CONTENTS_TITLE = re.compile(r"(table of )?contents|list of (figures|tables)|figures|tables", re.IGNORECASE)
CONTENTS_ENTRY = re.compile(
    r"(\.{2,}|\s)\d{1,3}$|^(\d+\.)+\d*\.?\s|^(appendix|part|chapter|section|figure|fig\.|table)\s",
    re.IGNORECASE,
)
CONTENTS_SHARE = 0.5  # a contents page has at least this share of entry-like blocks after its title...
CONTENTS_MORE_SHARE = 0.8  # ...and a page continuing it (no title) at least this share
MAX_CONTEXT_CHARS = 200  # a caption borrowed from the paragraph before a table is at most this long
SENTENCE_END = re.compile(r"(?<=[.!?:])\s+")
TOC_MIN_ENTRIES = 3  # an outline is used when it has at least this many entries...
TOC_MIN_LEVELS = 2  # ...on at least this many levels...
TOC_MIN_MATCHED = 0.7  # ...and at least this share of them is found as a heading on the page
TOC_REACH = 5  # an entry is looked for from the previous one's page up to this many pages past its own
TOC_SIMILAR = (
    0.85  # how alike an entry and its heading must be (difflib ratio), when neither starts the other
)
NUMBERING = re.compile(
    r"^((part|chapter|section|appendix)\s+)?([0-9]+|[ivxlc]+|[a-z])([.:)]|(\.[0-9]+)+\.?)?\s+"
)

TocEntry = tuple[int, str, int]  # (level, title, 1-based page), as pymupdf's get_toc gives them


@dataclass(frozen=True)
class Para:
    text: str
    page: int
    path: tuple[str, ...]  # the headings this paragraph sits under; a heading's own path ends with itself
    level: int | None = None  # none for body text; 0 is the top level
    table: Table | None = None  # set for a table, whose text is its markdown
    figure: Figure | None = None  # set for a figure, whose text is its description
    toc: bool = False  # a heading whose level came from the pdf outline

    @property
    def region(self) -> bool:
        """a table or a figure: a chunk of its own, never text."""
        return self.table is not None or self.figure is not None


def mostly_bold(pages: Pages) -> bool:
    chars = [(len(b.text), b.bold) for blocks in pages for b in blocks]
    return sum(n for n, bold in chars if bold) > sum(n for n, _ in chars) / 2


def _heading_size(block: Block, body: float, bold_body: bool) -> float | None:
    """the size that ranks this block among the headings, or none for body text."""
    if (
        len(block.text) > MAX_HEADING_CHARS
        or block.text[-1] in NOT_HEADING_ENDINGS
        or block.text[0] in BULLETS
    ):
        return None
    if block.size >= body * SIZE_RATIO:
        return block.size
    if block.bold and not bold_body and block.size >= body * BOLD_RATIO:
        return body  # every bold body-size heading shares the lowest level
    return None


def outline(
    pages: Pages,
    tables: list[list[Table]] | None = None,
    figures: list[list[Figure]] | None = None,
    toc: list[TocEntry] | None = None,
    columns: list[Columns | None] | None = None,
) -> list[Para]:
    """the document as paragraphs, headings, tables and figures in reading order. `columns[i]` is page i + 1's two-column layout (tables and figures are then placed by column, not just by height)."""
    pages = _without_contents(pages)
    body, bold_body = body_size(pages), mostly_bold(pages)
    raw = [_page_sizes(blocks, body, bold_body) for blocks in pages]
    sizes = _without_titles(raw)
    levels = _toc_levels(pages, raw, sizes, toc) if toc else None
    from_toc = levels is not None
    if levels is None:
        ranked = sorted({s for page in sizes for s in page if s is not None}, reverse=True)
        level_of = {s: min(i, MAX_LEVELS - 1) for i, s in enumerate(ranked)}
        levels = [[None if s is None else level_of[s] for s in page] for page in sizes]

    paras: list[Para] = []
    stack: list[tuple[int, str]] = []
    for page_no, (blocks, page_levels) in enumerate(zip(pages, levels, strict=False), start=1):
        prev: Block | None = None  # the previous body block on this page, while paras[-1] is its para
        prev_heading: Block | None = None  # the previous block, if it was a heading (paras[-1] is its para)
        cols = columns[page_no - 1] if columns and page_no <= len(columns) else None
        waiting: list[Table | Figure] = sorted(
            [
                *(tables[page_no - 1] if tables and page_no <= len(tables) else ()),
                *(figures[page_no - 1] if figures and page_no <= len(figures) else ()),
            ],
            key=lambda r: reading_key(cols, r.bbox),
        )
        for i, (block, level) in enumerate(zip(blocks, page_levels, strict=False)):
            here = reading_key(cols, (block.x0, block.top, block.x1, block.bottom))
            if waiting and reading_key(cols, waiting[0].bbox) <= here:
                while waiting and reading_key(cols, waiting[0].bbox) <= here:
                    paras.append(_region_para(waiting.pop(0), page_no, stack, paras))
                prev = prev_heading = None
            if level is None:
                prev_heading = None
                if i in (0, len(blocks) - 1) and PAGE_NUMBER.fullmatch(block.text):
                    continue
                if prev and _continues(prev, block):
                    paras[-1] = Para(join_lines(paras[-1].text, block.text), page_no, paras[-1].path)
                else:
                    paras.append(Para(block.text, page_no, tuple(t for _, t in stack)))
                prev = block
                continue
            prev = None
            if prev_heading and _wraps(prev_heading, block):
                text = join_lines(paras[-1].text, block.text)
                stack[-1] = (stack[-1][0], text)
                paras[-1] = Para(text, page_no, tuple(t for _, t in stack), paras[-1].level, toc=from_toc)
                prev_heading = block
                continue
            prev_heading = block
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block.text))
            paras.append(Para(block.text, page_no, tuple(t for _, t in stack), level, toc=from_toc))
        paras += [_region_para(r, page_no, stack, paras) for r in waiting]
    return paras


def _toc_levels(
    pages: Pages, raw: list[list[float | None]], sizes: list[list[float | None]], toc: list[TocEntry]
) -> list[list[int | None]] | None:
    """use matched outline levels when coverage is sufficient; otherwise retain style-based levels."""
    if len(toc) < TOC_MIN_ENTRIES or len({lvl for lvl, _, _ in toc}) < TOC_MIN_LEVELS:
        return None
    top = min(lvl for lvl, _, _ in toc)
    matched: dict[tuple[int, int], int] = {}  # (page index, block index) -> level
    wrapped: set[tuple[int, int]] = set()  # the second line of a listed heading that wraps
    cursor = (0, -1)  # entries are in reading order: each is found after the one before it
    for lvl, title, page in toc:
        found = _find_entry(pages, raw, _normal(title), page - 1, cursor)
        if found is not None:
            (p, i), joined = found
            matched[(p, i)] = lvl - top
            if joined:
                wrapped.add((p, i + 1))
            cursor = (p, i + 1) if joined else (p, i)
    if len(matched) < TOC_MIN_MATCHED * len(toc):
        return None
    smallest = min((size for p, i in matched if (size := raw[p][i]) is not None), default=0.0)
    below = sorted({s for row in sizes for s in row if s is not None and s < smallest}, reverse=True)
    deepest = max(matched.values()) + 1
    level_below = {s: min(deepest + i, max(deepest, MAX_LEVELS - 1)) for i, s in enumerate(below)}
    levels: list[list[int | None]] = []
    for p, page_sizes in enumerate(sizes[: len(pages)]):
        out: list[int | None] = []
        for i, s in enumerate(page_sizes):
            if (p, i) in matched:
                out.append(matched[(p, i)])
            elif (p, i) in wrapped:
                out.append(out[-1])  # outline() joins it to the heading line above
            else:
                out.append(None if s is None else level_below.get(s))
        levels.append(out)
    return levels


def _find_entry(
    pages: Pages, raw: list[list[float | None]], title: str, page: int, cursor: tuple[int, int]
) -> tuple[tuple[int, int], bool] | None:
    """match the next outline title to a heading candidate, including wrapped headings."""
    if not title or not pages:
        return None
    start = max(cursor[0], min(page, len(pages) - 1))
    for p in range(start, min(len(pages), max(page, start) + TOC_REACH + 1)):
        for i, block in enumerate(pages[p]):
            if (p, i) <= cursor or raw[p][i] is None:
                continue
            nxt = pages[p][i + 1] if i + 1 < len(pages[p]) else None
            texts = [_normal(block.text)]
            if nxt is not None and _wraps(block, nxt):
                texts.append(_normal(join_lines(block.text, nxt.text)))
            best = max(range(len(texts)), key=lambda k: SequenceMatcher(None, texts[k], title).ratio())
            if _alike(texts[best], title):
                return (p, i), best == 1  # the closer of the line alone and the line with the next one
    return None


def _normal(text: str) -> str:
    """casefolded, whitespace collapsed, and without leading numbering (\"3.2\", \"part 1:\", \"a.\")."""
    text = " ".join(text.casefold().split())
    return NUMBERING.sub("", text, count=1) or text


def _alike(a: str, b: str) -> bool:
    """the same title: one starts the other (most of it), or they are nearly the same text."""
    short, long = sorted((a, b), key=len)
    if len(short) >= 4 and long.startswith(short) and len(short) >= 0.6 * len(long):
        return True
    return SequenceMatcher(None, a, b).ratio() >= TOC_SIMILAR


def _region_para(
    region: Table | Figure, page_no: int, stack: list[tuple[int, str]], paras: list[Para]
) -> Para:
    # a figure keeps only a printed caption: on slides, the text before a figure is a bullet, not an
    # introduction
    if (
        not region.caption
        and isinstance(region, Table)
        and paras
        and paras[-1].level is None
        and not paras[-1].region
    ):
        context = SENTENCE_END.split(paras[-1].text.strip())[-1]
        if context and len(context) <= MAX_CONTEXT_CHARS:
            region = replace(region, caption=context)
    path = tuple(t for _, t in stack)
    if isinstance(region, Figure):
        return Para(region.description, page_no, path, None, figure=region)
    return Para(markdown(region), page_no, path, None, region)


def _page_sizes(blocks: list[Block], body: float, bold_body: bool) -> list[float | None]:
    """_heading_size per block, except that the wrapped rest of a body line is body text too."""
    sizes: list[float | None] = []
    for i, block in enumerate(blocks):
        continued = i > 0 and sizes[-1] is None and _continues(blocks[i - 1], block)
        sizes.append(None if continued else _heading_size(block, body, bold_body))
    return sizes


def _without_titles(sizes: list[list[float | None]]) -> list[list[float | None]]:
    """demote to body text the heading sizes that aren't section levels: those used on one page only, or only on the first front_pages pages (see the module docstring)."""
    if len(sizes) < MIN_TITLE_PAGES:
        return sizes
    pages_with: dict[float, set[int]] = defaultdict(set)
    for page_no, page in enumerate(sizes):
        for s in page:
            if s is not None:
                pages_with[s].add(page_no)
    levels = {s for s, on in pages_with.items() if len(on) > 1 and max(on) >= FRONT_PAGES}
    if not levels:
        return sizes
    return [[s if s in levels else None for s in page] for page in sizes]


def _without_contents(pages: Pages) -> Pages:
    """drop contents pages from their title on (see the module docstring)."""
    out: Pages = []
    continuing = False
    for blocks in pages:
        start = next((i for i, b in enumerate(blocks) if CONTENTS_TITLE.fullmatch(b.text.strip())), None)
        entries = blocks if start is None else blocks[start + 1 :]
        share = sum(1 for b in entries if CONTENTS_ENTRY.search(b.text)) / len(entries) if entries else 0.0
        continuing = (
            share >= CONTENTS_SHARE if start is not None else continuing and share >= CONTENTS_MORE_SHARE
        )
        out.append(blocks[: start or 0] if continuing else blocks)
    return out


def _wraps(prev: Block, block: Block) -> bool:
    """the second line of a heading: same style, not numbered as a heading of its own, and the joined text still short enough for a heading."""
    return (
        (prev.size, prev.bold) == (block.size, block.bold)
        and not NUMBERED.match(block.text)
        and len(prev.text) + len(block.text) < MAX_HEADING_CHARS
    )


def _continues(prev: Block, block: Block) -> bool:
    """a wrapped line: same style, starts in lower case, and the line before didn't end a sentence."""
    return (
        (prev.size, prev.bold) == (block.size, block.bold)
        and block.text[0].islower()
        and prev.text[-1] not in CONTINUATION_BREAKS
    )
