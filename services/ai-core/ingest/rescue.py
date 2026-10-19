"""Tables every extraction method rejected, laid out by the model from the PDF's own words.
Async; runs after the vision pass, before parsing.

The vision pass says where a table is; ingest.tables reads its cells from the text layer and keeps the grid
only when it matches what the model saw. About one table in eight fails that check (no rulings, irregular
spacing) and used to fall back to prose, where its rows are mangled. For each such table one more call shows the
model the table's crop and the PDF words inside it and asks it to arrange those words into cells. The answer is
kept only when `tables.verify_layout` finds it made of the box's words (nothing invented), using nearly all of
them (nothing dropped), and the shape the page call saw; its cells are then the PDF's own text, so the table is
source "pdf". A failed call or check leaves the table as prose, as before.

Cached like every vision answer, by page content, table index and prompt version.
"""

import logging
from dataclasses import replace
from pathlib import Path

import pymupdf
from fastapi import HTTPException

import pdf
from ingest import tables as extraction
from ingest.geometry import Word, center_in, pad
from ingest.pages import page_key, visible_words
from ingest.prompts import UNTRUSTED
from ingest.strings import clean_cell
from ingest.vision import MIN_TEXT_WORDS, PageRef, PageVision, SeenTable, items
from ingest.vision_calls import FIGURE_CONCURRENCY, _semaphore, ask, cached

log = logging.getLogger("ai-core.ingest.rescue")

LAYOUT_PROMPT_VERSION = 1
MAX_LAYOUT_WORDS = 1500  # PDF words passed with one table

LAYOUT_PROMPT = """This image is one table cut from a PDF page; below are the PDF's own words inside it, in reading
order. Arrange these words into the table's cells as the image shows them: the header row (if there is one) and
the body rows. Use the words exactly as given; do not correct, translate, summarise or add anything. Leave out a
caption, footnotes or a page header. Use "" for an empty cell; repeat a merged cell's text in each row it spans.
<pdf_words>{words}</pdf_words>
Return JSON only: {{"header": ["..."] or null, "rows": [["...", "..."], ...]}}

""" + UNTRUSTED.replace("{", "{{").replace("}", "}}")


async def rescue_tables(
    path: Path, seen: list[PageVision | None], *, model: str, cache_dir: Path
) -> list[PageVision | None]:
    """`seen` with a verified layout on each table the extraction methods rejected; unchanged otherwise."""
    todo = await pdf.in_thread(_rejected, path, seen)
    out = list(seen)
    for number, key, i, words in todo:
        page = out[number]
        assert page is not None  # _rejected only lists tables the vision pass saw
        table = page.tables[i]
        layout = await _laid_out(PageRef(path, number, key, cache_dir), i, table, words, model)
        if layout is not None:
            tables = list(page.tables)
            tables[i] = replace(table, layout=layout)
            out[number] = replace(page, tables=tables)
    return out


def _rejected(path: Path, seen: list[PageVision | None]) -> list[tuple[int, str, int, list[Word]]]:
    """(page index, page key, table index, the box's words) of each table with a text layer that every
    extraction method rejected."""
    out = []
    with pymupdf.open(path) as doc:
        for number, page_seen in enumerate(seen):
            if page_seen is None or not page_seen.tables or page_seen.transcript is not None:
                continue
            page = doc[number]
            rejected = {tuple(r["bbox"]) for r in extraction.page_tables(page, page_seen.tables).rejected}
            words = visible_words(page)
            for i, t in enumerate(page_seen.tables):
                inside = [w for w in words if center_in(w, pad(t.bbox))]
                if tuple(t.bbox) in rejected and t.transcript is None and len(inside) >= MIN_TEXT_WORDS:
                    out.append((number, page_key(page), i, inside))
    return out


async def _laid_out(
    page: PageRef, i: int, table: SeenTable, words: list[Word], model: str
) -> tuple[tuple[str, ...] | None, tuple[tuple[str, ...], ...]] | None:
    """The model's layout of the table, if it passes `verify_layout`; None otherwise (also when the call fails)."""
    text = [w[4] for w in words]
    prompt = LAYOUT_PROMPT.format(words=" ".join(text[:MAX_LAYOUT_WORDS]))
    try:
        raw = await cached(
            page.cache(model, f"t{i}-layout-v{LAYOUT_PROMPT_VERSION}"),
            lambda: ask(
                prompt,
                lambda: page.render(pad(table.bbox)),
                model,
                _semaphore("figure", FIGURE_CONCURRENCY),
            ),
            "layout",
        )
    except HTTPException:
        raise
    except Exception as e:
        log.warning("table_not_laid_out", extra={"table": i + 1, "page": str(page), "error": str(e)})
        return None
    header = tuple(clean_cell(c) for c in items(raw.get("header"))) or None
    rows = tuple(tuple(clean_cell(c) for c in row) for row in items(raw.get("rows")) if isinstance(row, list))
    problem = extraction.verify_layout(header, rows, words, table)
    if problem:
        log.info("table_stays_prose", extra={"table": i + 1, "page": str(page), "problem": problem})
        return None
    return header, rows
