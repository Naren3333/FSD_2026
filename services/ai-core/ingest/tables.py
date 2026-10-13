"""Table extraction (PLAN.md §6.1 ingest step 2, RAG_PLAN Iteration 10). Blocking: runs while parsing.

The vision pass (ingest.vision) says where each table is and what it looks like; the cells are read from
the PDF's own text layer inside each box, and a grid that doesn't match the model's description stays prose:
a missed table is indexed as prose like before, a wrong one would be a regression. The model never writes
cell text, except for a table that is a picture (there is no text layer to read): those come transcribed
from the vision pass and are marked source="vision".

A page without a vision result falls back to find_tables "lines", which the table audit found has no
false positives.

A table no method could read may come with a layout from ingest.rescue (Iteration 14): the model's
arrangement of the PDF's own words into cells, checked here (`verify_layout`) before it was accepted. It is used
only when every method fails, and is source "pdf": its cells are the text layer's words.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from collections.abc import Sequence
from itertools import zip_longest
from typing import Any

import pymupdf

from ingest.strings import clean_cell, join_lines, unligature
from ingest.vision import MIN_TEXT_WORDS, SeenTable, area, center_in, overlap, pad

Box = tuple[float, float, float, float]

MIN_ROWS, MIN_COLS = 2, 2
MAX_EMPTY_SHARE = 0.5  # of the cells, counting merged cells (None) as filled
MAX_PAGE_SHARE = 0.9  # a "table" covering the whole page is a layout box (fallback only)
MIN_COVER = 0.6  # the grid covers at least this share of the model's box, and vice versa (loosely)
COL_SLACK = 1  # the grid's column count may differ from the model's by this much
THIN = 2.5  # a filled shape this thin is a ruling line (RFC-style grey cell borders)

# Extraction methods to try, in order, per style the model reports; the first grid that matches wins.
#   lines:   find_tables "lines" in the box (plus thin filled shapes as lines)
#   aligned: text rows + the model's column count, split at whitespace gutters (_aligned)
METHODS: dict[str, tuple[str, ...]] = {
    "bordered": ("lines", "aligned"),
    "shaded": ("lines", "aligned"),
    "rules": ("aligned", "lines"),
    "borderless": ("aligned",),
}
GUTTER_ROWS = 0.7  # a column gutter is whitespace in at least this share of the rows (spanning rows allowed)
WRAP_GAP = 0.35  # a line closer than this (x line height) under the previous one continues its row
ROW_GAP = 0.45  # a table with line gaps this wide (x line height) as well as tight ones spaces its rows apart
WRAP_MIN_WORDS = 4  # ...and continues a cell of running text (at least this many words), not a number
PROSE_WORDS = 6  # a line of at least this many words...
GAP_FACTOR = 1.2  # ...with no gap wider than this (x word height) is prose, not a table row
MIN_CHECK_ROWS = 4  # the model's row count is only checked for tables at least this long...
ROW_RANGE = (
    0.5,
    2.5,
)  # ...and the grid's must be within this factor of it (+2): rows collapsed or split apart
RULE_SHARE = 0.6  # a rule under the header spans at least this share of the table's width
MAX_HEADER_LINES = 3  # a header spans at most this many lines...
MAX_HEADER_WORDS = 4  # ...of short cells...
NUMERIC = re.compile(r"\d[\d.,·%]*\d|\d")  # ...with no numbers in them
CAPTION = re.compile(r"(table|exhibit|figure|fig\.)\s*[\dA-Z]", re.IGNORECASE)
LAYOUT_FOUND = (
    0.98  # a layout's cell words found among the box's PDF words, at least; and it uses every one of
)
# them but the caption's: a word left out is how a shifted header looks (CLIP Table 11 lost "DTD", so every
# dataset name from there on sat over its neighbour's column)
LAYOUT_EMPTY = (
    0.8  # a laid-out table may be this empty (Attention's Table 3 lists only what each variant changes)
)
LAYOUT_PLACED = (
    0.5  # at least this share of its body cells must be found on the page, each nearest its own column
)
LINE_REACH = 0.7  # a word is on a row's line when its middle is this close to it (x the median word height)


@dataclass(frozen=True)
class Table:
    bbox: Box
    header: tuple[str, ...] | None
    rows: tuple[tuple[str, ...], ...]
    caption: str | None = None
    source: str = "pdf"  # "pdf": cells read from the text layer; "vision": transcribed from an image
    continues: bool = False  # the model says it goes on onto the next page
    last_page: int | None = None  # 1-based: the page a table joined across pages ends on


@dataclass
class PageResult:
    tables: list[Table] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # {"bbox", "reason"}: regions left as prose
    detections: list[dict] = field(default_factory=list)  # for the table audit: every grid considered


def page_tables(page: pymupdf.Page, seen: list[SeenTable] | None) -> PageResult:
    """The tables on a page, read from its text layer: inside the model's boxes, or with find_tables "lines"
    when there is no vision result for the page."""
    lines = thin_lines(page)
    if seen is None:
        return _fallback(page, lines)
    result = PageResult()
    for s in seen:
        clip = _clip(page, s.bbox)
        if s.transcript is not None or _words_in(page, clip) < MIN_TEXT_WORDS:
            table = _transcribed(s)
            if table:
                result.tables.append(table)
            else:
                result.rejected.append({"bbox": s.bbox, "reason": "picture of a table without a transcript"})
            continue
        reasons = []
        for method in METHODS.get(s.style, METHODS["bordered"]):
            grid = _ruled(page, clip, s, lines) if method == "lines" else _aligned(page, clip, s)
            reason = _verify(grid, s)
            result.detections.append(
                {"bbox": s.bbox, "method": method, "grid": grid and grid["bbox"], "reason": reason}
            )
            if reason is None and grid is not None:
                result.tables.append(_table(grid, s))
                break
            reasons.append(f"{method}: {reason}")
        else:
            if s.layout is not None:  # checked by verify_layout when it was made
                result.tables.append(
                    Table(s.bbox, s.layout[0], s.layout[1], s.caption, "pdf", s.continues_on_next)
                )
                continue
            result.rejected.append({"bbox": s.bbox, "reason": "; ".join(reasons)})
    return result


def _ruled(page: pymupdf.Page, clip: Box, s: SeenTable, lines: list) -> dict | None:
    """find_tables "lines" inside the box, with the page's thin filled shapes added as lines."""
    extra = [ln for ln in lines if _within(ln, clip)] or None
    grids = page.find_tables(clip=pymupdf.Rect(clip), strategy="lines", add_lines=extra).tables
    best = max(grids, key=lambda g: overlap(tuple(g.bbox), s.bbox), default=None)
    return _grid(best) if best else None


def _aligned(page: pymupdf.Page, clip: Box, s: SeenTable) -> dict | None:
    """Rows from text lines and the model's number of columns, split where (nearly) every row has
    whitespace. For tables without vertical lines, where find_tables "text" splits cells apart."""
    if s.cols < MIN_COLS:
        return None
    words = [w for w in page.get_text("words") if center_in(w, clip)]  # whole words: clip= cuts them
    rows = _trim_prose(_text_rows(words, s.caption), s.cols)
    if len(rows) < MIN_ROWS:
        return None
    x0, x1 = min(w[0] for r in rows for w in r["words"]), max(w[2] for r in rows for w in r["words"])
    width = int(x1 - x0)
    if width <= 0:
        return None
    free = [0] * width  # rows with no word over each 1pt column of x
    for r in rows:
        used = [False] * width
        for w in r["words"]:
            for x in range(max(0, int(w[0] - x0) - 1), min(width, int(w[2] - x0) + 1)):
                used[x] = True
        for x in range(width):
            free[x] += not used[x]
    need = GUTTER_ROWS * len(rows)
    gaps, start = [], None
    for x in range(width + 1):
        if x < width and free[x] >= need:
            start = x if start is None else start
        elif start is not None:
            gaps.append((x - start, start, x))
            start = None

    # a gutter has words on both sides in many rows; the ragged right edge of a wrapped text column doesn't
    def sides(a: float, b: float) -> int:
        return sum(
            1
            for r in rows
            if any(w[2] - x0 <= a + 1 for w in r["words"]) and any(w[0] - x0 >= b - 1 for w in r["words"])
        )

    gaps = sorted(
        sorted(gaps, key=lambda g: (sides(g[1], g[2]), g[0]), reverse=True)[: s.cols - 1], key=lambda g: g[1]
    )
    if len(gaps) < s.cols - 1:
        return None
    cuts = [x0 + (a + b) / 2 for _, a, b in gaps]
    cells = []
    for r in rows:
        cols: list[list[str]] = [[] for _ in range(s.cols)]
        for w in r["words"]:
            cols[sum(1 for c in cuts if (w[0] + w[2]) / 2 > c)].append(w[4])
        cells.append(
            {"cells": [unligature(" ".join(c)) for c in cols], "top": r["top"], "bottom": r["bottom"]}
        )
    lines = _header_lines(page, (x0, rows[0]["top"], x1, rows[-1]["bottom"]), cells)
    if lines:  # a rule under the header (booktabs' midrule): everything above it is header
        header = tuple(" ".join(x for x in col if x) for col in zip(*(c["cells"] for c in cells[:lines])))
        body = [tuple(r) for r in _merge_wrapped(cells[lines:])]
        merged: list[Sequence[str]] = [list(header), *body]
    else:
        merged = list(_merge_wrapped(cells))
        header, body = _header_rows(tuple(merged[0]), [tuple(r) for r in merged[1:]])
    filled = sum(1 for row in merged for c in row if c)
    total = sum(len(row) for row in merged) or 1
    box = (x0, rows[0]["top"], x1, rows[-1]["bottom"])
    return {
        "bbox": box,
        "rows": tuple(tuple(r) for r in body),
        "header": header if any(header) else None,
        "n_rows": len(merged),
        "n_cols": s.cols,
        "empty_share": 1 - filled / total,
        "merged": 0,
    }


def _text_rows(words: list, caption: str | None) -> list[dict]:
    """Words grouped into visual lines by vertical centre; a line that is the table's caption is dropped."""
    rows: list[dict] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        mid, h = (w[1] + w[3]) / 2, w[3] - w[1]
        if rows and abs(mid - rows[-1]["mid"]) <= 0.5 * h:
            rows[-1]["words"].append(w)
            rows[-1]["bottom"] = max(rows[-1]["bottom"], w[3])
        else:
            rows.append({"mid": mid, "top": w[1], "bottom": w[3], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w[0])
    if caption:
        key = unligature(" ".join(caption.split()[:4])).lower()
        rows = [r for r in rows if not unligature(" ".join(w[4] for w in r["words"])).lower().startswith(key)]
    return rows


def _trim_prose(rows: list[dict], cols: int) -> list[dict]:
    """Drop the lines at the top and bottom of the box that read as prose rather than table rows: a
    caption, a page header, footnotes. A table row has a word gap wider than ordinary spacing; with 3+
    columns, a line spanning most of the width with no such gap is prose."""

    def prose(r: dict) -> bool:
        ws = r["words"]
        height = max(w[3] - w[1] for w in ws)
        gap = max((b[0] - a[2] for a, b in zip(ws, ws[1:])), default=0.0)
        return (
            len(ws) >= PROSE_WORDS
            and gap < GAP_FACTOR * height
            and (cols >= 3 or bool(CAPTION.match(_line(r))))
        )

    while rows and (prose(rows[0]) or CAPTION.match(_line(rows[0]))):
        rows = rows[1:]
    while rows and prose(rows[-1]):
        rows = rows[:-1]
    return rows


def _line(r: dict) -> str:
    return " ".join(w[4] for w in r["words"])


def _header_lines(page: pymupdf.Page, box: Box, lines: list[dict]) -> int:
    """How many text lines sit above the table's first rule that has lines both above and below it (the
    rule under the header), or 0 when there is none within MAX_HEADER_LINES lines of the top."""
    width = box[2] - box[0]
    rules = sorted(
        y
        for y, x0, x1 in _horizontal_rules(page)
        if x1 - x0 >= RULE_SHARE * width and x0 <= box[0] + width * (1 - RULE_SHARE) and box[1] < y < box[3]
    )
    for y in rules:
        above = sum(1 for ln in lines if ln["bottom"] <= y + 1)
        if 0 < above < len(lines):
            return above if above <= MAX_HEADER_LINES else 0
    return 0


def _horizontal_rules(page: pymupdf.Page) -> list[tuple[float, float, float]]:
    """(y, x0, x1) of the page's horizontal lines: stroked lines, and thin rects stroked or filled."""
    out = []
    for path in page.get_drawings():
        for item in path["items"]:
            if item[0] == "l" and abs(item[1].y - item[2].y) <= 1:
                out.append(
                    ((item[1].y + item[2].y) / 2, min(item[1].x, item[2].x), max(item[1].x, item[2].x))
                )
            elif item[0] == "re" and item[1].height <= THIN:
                out.append(((item[1].y0 + item[1].y1) / 2, item[1].x0, item[1].x1))
    return out


def _header_rows(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> tuple[tuple[str, ...], list]:
    """Fold a header that runs over several lines into one: a following line with no numbers joins it when it
    is sparse ("Sequential" over "Operations") or the header so far has gaps (a spanning "BLEU" over
    "EN-DE | EN-FR"). At most MAX_HEADER_LINES lines."""
    for _ in range(MAX_HEADER_LINES - 1):
        if not rows:
            break
        row = rows[0]
        filled = [c for c in row if c]
        if not filled or any(NUMERIC.search(c) or len(c.split()) > MAX_HEADER_WORDS for c in filled):
            break
        if not (len(filled) <= len(row) / 2 or (len(header) == len(row) and not all(header))):
            break
        header = tuple(" ".join(x for x in (h, c) if x) for h, c in zip_longest(header, row, fillvalue=""))
        rows = rows[1:]
    return header, rows


def _merge_wrapped(rows: list[dict]) -> list[list[str]]:
    """A line that continues the row above it is merged into it: it sits tight under the previous line and
    carries on a cell of running text (without that, the rows of a group under one row label, "Finetune":
    5 models each with numbers, would collapse into one). Its first cell must be empty too, unless the
    table spaces its rows apart: then a tight line is a wrapped line in every column."""
    gaps = [b["top"] - a["bottom"] for a, b in zip(rows, rows[1:])]
    height = sorted(r["bottom"] - r["top"] for r in rows)[len(rows) // 2] if rows else 0.0
    spaced = any(g < WRAP_GAP * height for g in gaps) and any(g >= ROW_GAP * height for g in gaps)
    out: list[list[str]] = []
    prev = None
    for r in rows:
        tight = prev is not None and r["top"] - prev["bottom"] < WRAP_GAP * (prev["bottom"] - prev["top"])
        running = out and any(
            c and len(out[-1][i].split()) >= WRAP_MIN_WORDS for i, c in enumerate(r["cells"])
        )
        if out and len(out) > 1 and tight and running and (spaced or not r["cells"][0]):
            out[-1] = [join_lines(a, b) if a and b else a or b for a, b in zip(out[-1], r["cells"])]
        else:
            out.append(list(r["cells"]))
        prev = r
    return out


def thin_lines(page: pymupdf.Page) -> list[tuple[pymupdf.Point, pymupdf.Point]]:
    """Ruling lines drawn as thin filled shapes, which find_tables doesn't treat as lines by itself."""
    out = []
    for path in page.get_drawings():
        if path.get("fill") is None or path.get("color") is not None:
            continue  # stroked paths are already lines to find_tables
        r = path["rect"]
        if r.height <= THIN and r.width >= 10:
            y = (r.y0 + r.y1) / 2
            out.append((pymupdf.Point(r.x0, y), pymupdf.Point(r.x1, y)))
        elif r.width <= THIN and r.height >= 8:
            x = (r.x0 + r.x1) / 2
            out.append((pymupdf.Point(x, r.y0), pymupdf.Point(x, r.y1)))
    return out


def _fallback(page: pymupdf.Page, lines: list) -> PageResult:
    result = PageResult()
    page_area = area(tuple(page.rect))
    for g in page.find_tables(strategy="lines", add_lines=lines or None).tables:
        grid = _grid(g)
        reason = _shape_problem(grid) or (
            "covers the whole page (layout box)" if area(grid["bbox"]) > MAX_PAGE_SHARE * page_area else None
        )
        result.detections.append({"bbox": grid["bbox"], "grid": grid["bbox"], "reason": reason})
        if reason:
            result.rejected.append({"bbox": grid["bbox"], "reason": reason})
        else:
            result.tables.append(Table(grid["bbox"], grid["header"], grid["rows"]))
    return result


def _grid(tab: Any) -> dict:
    cells = _without_empty_columns(tab.extract())
    merged = sum(1 for row in cells for c in row if c is None)
    empty = sum(1 for row in cells for c in row if c is not None and not c.strip())
    rows = _fill_down([tuple(clean_cell(c) for c in row) for row in cells], cells)
    header: tuple[str, ...] | None = None
    if tab.header.external:
        header = tuple(clean_cell(n) for n in tab.header.names)
    elif rows:
        header, rows = _header_rows(rows[0], rows[1:])
    if header is not None and not any(header):
        header = None
    total = sum(len(r) for r in cells) or 1
    return {
        "bbox": tuple(tab.bbox),
        "rows": tuple(rows),
        "header": header,
        "n_rows": tab.row_count,
        "n_cols": max((len(r) for r in cells), default=0),
        "empty_share": empty / total,
        "merged": merged,
    }


def _without_empty_columns(cells: list[list]) -> list[list]:
    """find_tables makes a column for every vertical line, so a ruled-off margin is an empty column, and a
    stray line through a header cell splits it ("Last na" | "me"). A column empty below the first row is
    dropped; its header text joins the column on its left."""
    width = max((len(r) for r in cells), default=0)
    rows = [[(r[i] if i < len(r) else "") for i in range(width)] for r in cells]
    keep: list[int] = []
    for i in range(width):
        header = bool(rows) and bool((rows[0][i] or "").strip())
        if any((r[i] or "").strip() for r in rows[1:]) or (header and not keep):
            keep.append(i)
        elif header:
            left = keep[-1]
            rows[0][left] = " ".join(x for x in ((rows[0][left] or "").strip(), rows[0][i].strip()) if x)
    return (
        [[r[i] for i in keep] for r in rows] if any(any((c or "").strip() for c in r) for r in rows) else []
    )


def _fill_down(rows: list[tuple[str, ...]], raw: list[list]) -> list[tuple[str, ...]]:
    """A merged cell in the first column (a row group, e.g. a rubric criterion) is repeated down its rows."""
    out: list[tuple[str, ...]] = []
    for i, row in enumerate(rows):
        if i and out and raw[i] and raw[i][0] is None and row and not row[0]:
            row = (out[-1][0], *row[1:])
        out.append(row)
    return out


def _shape_problem(grid: dict) -> str | None:
    if grid["n_rows"] < MIN_ROWS or grid["n_cols"] < MIN_COLS:
        return f"{grid['n_rows']}x{grid['n_cols']} is too small"
    if grid["empty_share"] > MAX_EMPTY_SHARE:
        return f"{grid['empty_share']:.0%} of the cells are empty"
    return None


def _verify(grid: dict | None, s: SeenTable) -> str | None:
    """Why the grid doesn't match what the model saw (None: it does)."""
    if grid is None:
        return "no grid found in the box"
    if problem := _shape_problem(grid):
        return problem
    if overlap(grid["bbox"], s.bbox) < MIN_COVER:
        return "the grid covers only part of the box"
    if s.cols and abs(grid["n_cols"] - s.cols) > COL_SLACK:
        return f"{grid['n_cols']} columns, the model saw {s.cols}"
    if s.rows >= MIN_CHECK_ROWS and not ROW_RANGE[0] * s.rows <= grid["n_rows"] <= ROW_RANGE[1] * s.rows + 2:
        return f"{grid['n_rows']} rows, the model saw {s.rows}"
    return None


def verify_layout(
    header: tuple[str, ...] | None, rows: tuple[tuple[str, ...], ...], words: list, s: SeenTable
) -> str | None:
    """Why a model's layout of a table can't be trusted (None: it can). `words` are the box's PDF words (box +
    text). The cells must be made of those words (a word hyphenated over two lines may be joined) and use every one
    of them but the caption's; and the body cells must sit on the page where the layout puts them: each one found,
    nearest the column the layout gives it (`_misplaced`). Words are compared casefolded, without the punctuation
    around them."""
    grid = [list(header)] + [list(r) for r in rows] if header else [list(r) for r in rows]
    if len(grid) < MIN_ROWS or max((len(r) for r in grid), default=0) < MIN_COLS:
        return "too small"
    cells = [c for r in grid for c in r]
    if sum(1 for c in cells if not c) / len(cells) > LAYOUT_EMPTY:
        return "mostly empty"
    box = [_token(w[4]) for w in words]
    # a word hyphenated over two lines, or set as two (a footnote mark: "Unicoder-VL" + "a" for "Unicoder-VLᵃ")
    known = (
        set(box)
        | {a[:-1] + b for a, b in zip(box, box[1:]) if a.endswith("-")}
        | {a + b for a, b in zip(box, box[1:])}
    )
    used = [_token(w) for c in cells for w in c.split()]
    used = [w for w in used if w]
    if not used:
        return "no words"
    found = sum(1 for w in used if w in known) / len(used)
    if found < LAYOUT_FOUND:
        return f"{1 - found:.0%} of its words are not in the box"
    caption = {_token(w) for w in (s.caption or "").split()}
    wanted = {w for w in box if w and w not in caption}
    taken = set(used) | {w for w in wanted if any(w in u for u in used)}  # parts of a joined hyphenated word
    if missing := sorted(wanted - taken):
        return f"leaves out {len(missing)} of the box's words ({', '.join(missing[:5])})"
    return _misplaced([list(r) for r in rows], words)


def _misplaced(rows: list[list[str]], words: list) -> str | None:
    """Why the body cells don't sit where the layout puts them (None: they do). A cell is found by its first word;
    a word printed more than once is taken from the row's own line, whose height comes from the row's words found
    only once in the box and in no other row. A found cell must be nearer its own column's middle than any other
    column's: a value moved one empty cell over (Attention's Table 3) is nearer its neighbour's."""
    at: dict[str, list[tuple[float, float]]] = {}
    for w in words:
        at.setdefault(_token(w[4]), []).append(((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))
    heights = sorted(w[3] - w[1] for w in words)
    reach = LINE_REACH * heights[len(heights) // 2] if heights else 0.0  # the same line: table rows sit close
    in_rows: dict[
        str, int
    ] = {}  # how many layout rows a word starts a cell in (a merged cell repeats down its rows)
    for row in rows:
        for t in {_token(c.split()[0]) for c in row if c.strip()}:
            in_rows[t] = in_rows.get(t, 0) + 1
    placed: list[tuple[int, float]] = []  # (column, x) of each cell found
    cells = 0
    for row in rows:
        firsts = [(j, _token(c.split()[0])) for j, c in enumerate(row) if c.strip()]
        cells += len(firsts)
        unique = [at[t][0][1] for _, t in firsts if len(at.get(t, ())) == 1 and in_rows[t] == 1]
        if not unique:
            continue
        y = sorted(unique)[len(unique) // 2]
        # each word's spots on the row's line, left to right: a word printed several times in the row ("-" for no
        # result) takes them in the cells' order
        line = {t: sorted(x for x, wy in at.get(t, ()) if abs(wy - y) <= reach) for _, t in firsts}
        for j, t in firsts:
            if line[t]:
                placed.append((j, line[t].pop(0)))
    if not cells or len(placed) < LAYOUT_PLACED * cells:
        return f"only {len(placed)} of {cells} cells found on the page"
    middle = {
        j: sorted(x for k, x in placed if k == j)[len([1 for k, _ in placed if k == j]) // 2]
        for j in {k for k, _ in placed}
    }
    wrong = [(j, x) for j, x in placed if min(middle, key=lambda k: abs(middle[k] - x)) != j]
    if wrong:
        return f"{len(wrong)} cells sit under another column"
    return None


def _token(word: str) -> str:
    """A word as compared by verify_layout: compatibility-normalised (superscripts as plain letters), casefolded,
    without the punctuation around it. Only for the comparison: the cells keep the text as written."""
    return unicodedata.normalize("NFKC", word).strip(".,;:!?()[]{}\"'“”‘’").casefold()


def _table(grid: dict, s: SeenTable) -> Table:
    return Table(grid["bbox"], grid["header"], grid["rows"], s.caption, "pdf", s.continues_on_next)


def _transcribed(s: SeenTable) -> Table | None:
    if not s.transcript or len(s.transcript) < 2:
        return None
    header, *rows = s.transcript
    cells = [c for r in rows for c in r]
    if not cells or sum(1 for c in cells if c) / len(cells) < 1 - MAX_EMPTY_SHARE:
        return None  # the model couldn't read it either
    return Table(s.bbox, header or None, tuple(rows), s.caption, "vision", s.continues_on_next)


def width(table: Table) -> int:
    return max([len(table.header or ())] + [len(r) for r in table.rows])


def markdown(table: Table) -> str:
    """The table as a Markdown grid (a missing header becomes an empty one: Markdown needs a header row)."""
    return "\n".join(markdown_lines(table))


def markdown_lines(table: Table, rows: tuple[tuple[str, ...], ...] | None = None) -> list[str]:
    """Header, separator and one line per row (all of the table's rows, or the ones given)."""
    n = width(table)

    def row(cells: tuple[str, ...]) -> str:
        padded = [*cells, *[""] * (n - len(cells))]
        return "| " + " | ".join(c.replace("|", "\\|") or " " for c in padded) + " |"

    return [
        row(table.header or ()),
        "|" + "---|" * n,
        *(row(r) for r in (table.rows if rows is None else rows)),
    ]


def _clip(page: pymupdf.Page, box: Box) -> Box:
    r = pymupdf.Rect(pad(box)) & page.rect
    return tuple(r)


def _within(line: tuple, box: Box) -> bool:
    (p, q) = line
    return (
        box[0] - 1 <= min(p.x, q.x)
        and max(p.x, q.x) <= box[2] + 1
        and box[1] - 1 <= min(p.y, q.y) <= box[3] + 1
    )


def _words_in(page: pymupdf.Page, box: Box) -> int:
    return len(page.get_text("words", clip=pymupdf.Rect(box)))
