"""The ingest's vision pass: one call per page shows the rendered page to a vision model, which lists its tables
and figures. Async; runs before parsing.

Tables are then read from the PDF's text layer (ingest.tables). A table that is a picture (no text to read) is
transcribed here, by a second call on its crop. Each informative figure is described here too: its crop, plus the
PDF words inside it as grounding, go to the figure model, which may be a different one. Before that its box is
snapped to its content (ingest.snap), and a numbered figure gets the document's sentences that cite it
(ingest.figure_context). Photos and illustrations, mostly clip art in slide decks, are described only when they
have a printed caption or such a citing sentence: a captioned photo in a textbook carries content.

A page without a text layer (ingest.pages) is transcribed by one more call, whose blocks the parser uses as the
page's text; its tables come back in the transcript. A picture of text on a page that does have a text layer (a
scanned slide under typed text) is left to the figure pass.

Every call is cached and rate limited (ingest.vision_calls), with its prompt from ingest.prompts. A page the model
didn't look at (vision off, past the page cap, or the call failed) is None, and the parser falls back to
find_tables "lines" for it; a vision failure never fails the ingest.
"""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pymupdf

import pdf
from ingest.figure_context import figure_context, page_text
from ingest.geometry import FULL_PAGE_SHARE, TABLE_OVERLAP, Box, Word, area, center_in, overlap, pad
from ingest.pages import _render, has_text_layer, page_key, visible_words
from ingest.prompts import (
    DESCRIBE_PROMPT,
    DESCRIBE_PROMPT_VERSION,
    OCR_PROMPT,
    OCR_PROMPT_VERSION,
    PROMPT,
    TRANSCRIBE_PROMPT,
    TRANSCRIBE_PROMPT_VERSION,
)
from ingest.snap import geometry, snap
from ingest.strings import clean_cell
from ingest.vision_calls import FIGURE_CONCURRENCY, _semaphore, ask, cache_file, cached

log = logging.getLogger("ai-core.ingest.vision")


MIN_TEXT_WORDS = 3  # fewer PDF words than this in a table's box: it is a picture of a table


MIN_FIGURE_SHARE = 0.015  # a figure smaller than this share of the page isn't described (bullets, icons)


# illustrations and photos: the figure review found them mostly clip art (~18% worth keeping)
NOT_DESCRIBED = ("logo", "icon", "decoration")


CAPTIONED_ONLY = ("illustration", "photo")  # described only with a printed caption or a citing sentence


MAX_GROUND_WORDS = 400  # PDF words passed to the description call


@dataclass(frozen=True)
class SeenTable:
    """One table as the model describes it. bbox in page points, top-left origin."""

    bbox: Box
    rows: int
    cols: int
    style: str
    header: tuple[str, ...] = ()
    caption: str | None = None
    continues_on_next: bool = False
    transcript: tuple[tuple[str, ...], ...] | None = None  # pictures of tables only: header row first
    # a table every extraction method rejected, laid out by the model from the PDF's words (ingest.rescue)
    layout: tuple[tuple[str, ...] | None, tuple[tuple[str, ...], ...]] | None = None

    @classmethod
    def from_json(cls, d: dict, width: float, height: float) -> "SeenTable | None":
        box = _box(d, width, height)
        try:
            rows, cols = int(d.get("rows") or 0), int(d.get("cols") or 0)
        except (TypeError, ValueError):
            return None
        if box is None:
            return None
        return cls(
            box,
            rows,
            cols,
            str(d.get("style") or ""),
            tuple(str(h) for h in items(d.get("header"))),
            text_or_none(d.get("caption")),
            bool(d.get("continues_on_next_page")),
        )


@dataclass(frozen=True)
class SeenFigure:
    """One figure as the model describes it; `description` is set once it has been described."""

    bbox: Box
    type: str
    informative: bool
    caption: str | None = None
    title: str | None = None
    description: str | None = None
    values: tuple[str, ...] = ()
    words: str = ""  # the PDF's own words inside the figure, in reading order
    png: bytes | None = field(default=None, repr=False)  # the crop that was described

    @classmethod
    def from_json(cls, d: dict, width: float, height: float) -> "SeenFigure | None":
        box = _box(d, width, height)
        if box is None:
            return None
        return cls(
            box,
            str(d.get("type") or ""),
            bool(d.get("informative")),
            text_or_none(d.get("caption")),
            text_or_none(d.get("title")),
        )


@dataclass(frozen=True)
class SeenText:
    """One block of a page transcript (OCR): a heading (level 1-3), a paragraph, or a table."""

    kind: str  # "heading" | "text" | "table"
    text: str = ""
    level: int = 1
    header: tuple[str, ...] | None = None  # tables only
    rows: tuple[tuple[str, ...], ...] = ()  # tables only

    @classmethod
    def from_json(cls, d: dict) -> "SeenText | None":
        kind = d.get("kind")
        if kind == "table":
            rows = tuple(
                tuple(clean_cell(c) for c in row) for row in items(d.get("rows")) if isinstance(row, list)
            )
            header = tuple(clean_cell(c) for c in items(d.get("header"))) or None
            if not any(c for row in rows for c in row):
                return None
            return cls("table", header=header, rows=rows)
        text = " ".join(str(d.get("text") or "").split())
        if kind not in ("heading", "text") or not text:
            return None
        try:
            level = min(3, max(1, int(d.get("level") or 1)))
        except (TypeError, ValueError):
            level = 1
        return cls(kind, text, level)


@dataclass(frozen=True)
class PageVision:
    tables: list[SeenTable] = field(default_factory=list)
    figures: list[SeenFigure] = field(default_factory=list)
    transcript: tuple[SeenText, ...] | None = None  # set for a page without a text layer (OCR)


def text_or_none(value: object) -> str | None:
    """A text field of a model answer: a number becomes its text, anything else that isn't text (a list, an
    object) None. The answers are cached, so one odd field must not fail the same page on every reindex."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text = str(value)
    return text if text.strip() else None


def items(value: object) -> list:
    """A list field of a model answer; [] when the model sent something else."""
    return value if isinstance(value, list) else []


def _box(d: dict, width: float, height: float) -> Box | None:
    try:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in d["bbox"])
    except (KeyError, TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0 * width, y0 * height, x1 * width, y1 * height)


@dataclass(frozen=True)
class PageRef:
    """One page of the PDF being looked at: the file, the page's index, and where its model answers are cached."""

    path: Path
    number: int
    key: str  # page_key: what the page shows
    cache_dir: Path

    def cache(self, model: str, kind: str = "page") -> Path:
        return cache_file(self.cache_dir, model, self.key, kind)

    def render(self, clip: Box | None = None, sharp: bool = False) -> Awaitable[bytes]:
        return pdf.in_thread(_render, self.path, self.number, clip, sharp)

    def __str__(self) -> str:
        return f"{self.path.name} page {self.number + 1}"


@dataclass(frozen=True)
class VisionModels:
    """Which model makes each kind of call; None turns that kind off."""

    page: str
    figure: str | None = None  # describes figures
    ocr: str | None = None  # transcribes pages without a text layer


async def see_pages(
    path: Path,
    *,
    model: str,
    cache_dir: Path,
    max_pages: int,
    figure_model: str | None = None,
    ocr_model: str | None = None,
) -> list[PageVision | None]:
    """Per page, what the model sees; None for a page it didn't look at. Pages are rendered only for a call
    the cache can't answer, in a worker thread, one at a time per vision slot. With `figure_model`, the
    page's informative figures come back described; with `ocr_model`, a page without a text layer comes back
    with its transcript."""
    sizes, words, keys, texts = await pdf.in_thread(_layout, path, max_pages)
    results: list[PageVision | None] = [None] * len(sizes)
    models = VisionModels(model, figure_model, ocr_model)
    seen = await asyncio.gather(
        *(
            _see_page(PageRef(path, n, keys[n], cache_dir), sizes[n], words[n], models, texts)
            for n in range(min(max_pages, len(sizes)))
        )
    )
    for number, page in enumerate(seen):
        results[number] = page
    return results


def _layout(
    path: Path, max_pages: int
) -> tuple[list[tuple[float, float]], list[list[Word]], list[str], list[list[str]]]:
    """Every page's size and text (for figure context), and, for the pages the model looks at, the visible words
    (box + text, reading order) and the page key."""
    with pymupdf.open(path) as doc:
        sizes = [(p.rect.width, p.rect.height) for p in doc]
        texts = [page_text(p) for p in doc]
        pages = range(min(max_pages, doc.page_count))
        words = [visible_words(doc[n]) for n in pages]
        keys = [page_key(doc[n]) for n in pages]
    return sizes, words, keys, texts


async def _see_page(
    page: PageRef,
    size: tuple[float, float],
    words: list[Word],
    models: VisionModels,
    texts: Sequence[Sequence[str]] = (),
) -> PageVision | None:
    width, height = size
    # a page without a text layer is transcribed whole, before (and whatever becomes of) the page call
    transcript = (
        await _transcribed_page(page, models.ocr) if models.ocr and not has_text_layer(words) else None
    )
    try:
        raw = await cached(page.cache(models.page), lambda: ask(PROMPT, page.render, models.page), "page")
        tables = [
            t
            for d in items(raw.get("tables"))
            if isinstance(d, dict) and (t := SeenTable.from_json(d, width, height))
        ]
        figures = [
            f
            for d in items(raw.get("figures"))
            if isinstance(d, dict) and (f := SeenFigure.from_json(d, width, height))
        ]
        if transcript is not None:
            # the transcript has the page's tables; a figure the size of the page is the scan
            tables = []
            figures = [f for f in figures if area(f.bbox) < FULL_PAGE_SHARE * width * height]
        # a picture of a table has no text to read, whatever style the model gives it: transcribe it instead
        tables = [
            await _with_transcript(page, i, t, models.page) if _is_picture(t, words) else t
            for i, t in enumerate(tables)
        ]
    except Exception as e:  # never fail the ingest over vision
        log.warning("vision_failed_using_lines", extra={"page": str(page), "error": str(e)})
        return None if transcript is None else PageVision(transcript=transcript)
    if models.figure:
        # each one scans the whole document's text: off the event loop
        contexts = await asyncio.to_thread(
            lambda: [figure_context(texts, f.caption, page.number) for f in figures]
        )
        todo = [i for i, f in enumerate(figures) if describable(f, tables, width * height, contexts[i])]
        if todo:
            graphics, lines = await pdf.in_thread(geometry, page.path, page.number)
            boxes = [f.bbox for f in figures]
            for i in todo:
                blockers = [t.bbox for t in tables] + [b for j, b in enumerate(boxes) if j != i]
                figures[i] = replace(figures[i], bbox=snap(figures[i].bbox, graphics, lines, blockers, size))
        figures = [
            await _described(page, i, f, words, models.figure, contexts[i]) if i in todo else f
            for i, f in enumerate(figures)
        ]
    return PageVision(tables, figures, transcript)


async def _transcribed_page(page: PageRef, model: str) -> tuple[SeenText, ...] | None:
    """The page's text as the OCR model reads it, or None if the call fails (the page then has no text)."""
    try:
        raw = await cached(
            page.cache(model, f"ocr-v{OCR_PROMPT_VERSION}"),
            lambda: ask(
                OCR_PROMPT,
                lambda: page.render(sharp=True),
                model,
                _semaphore("figure", FIGURE_CONCURRENCY),
            ),
            "ocr",
        )
    except Exception as e:
        log.warning("page_not_transcribed", extra={"page": str(page), "error": str(e)})
        return None
    return tuple(b for d in items(raw.get("blocks")) if isinstance(d, dict) and (b := SeenText.from_json(d)))


def describable(f: SeenFigure, tables: list[SeenTable], page_area: float, context: str = "") -> bool:
    """Worth describing: informative, not a logo, icon or decoration, a photo or illustration only with a printed
    caption or a citing sentence, not tiny, and not a table the model also reported (a region is a table or a
    figure, never both)."""
    return (
        f.informative
        and f.type not in NOT_DESCRIBED
        and (f.type not in CAPTIONED_ONLY or bool(f.caption or context))
        and area(f.bbox) >= MIN_FIGURE_SHARE * page_area
        and not any(overlap(f.bbox, t.bbox) >= TABLE_OVERLAP for t in tables)
    )


async def _described(
    page: PageRef, i: int, f: SeenFigure, words: list[Word], model: str, context: str = ""
) -> SeenFigure:
    """The figure with its description, crop and PDF words; unchanged (so not indexed) if the call fails or
    the model finds nothing useful in it."""
    inside = " ".join(w[4] for w in words if center_in(w, pad(f.bbox)))
    try:
        png = await page.render(pad(f.bbox))
        caption = f' (caption: "{f.caption}")' if f.caption else ""
        prompt = DESCRIBE_PROMPT.format(
            type=f.type or "figure",
            caption=caption,
            words=" ".join(inside.split()[:MAX_GROUND_WORDS]) or "(none)",
            context=context or "(none)",
        )
        # the crop is the snapped box, and the context comes from elsewhere in the file (the same page in another
        # document may be cited differently): both name the answer, so neither can be reused for another
        what = hashlib.sha256(f"{[round(v, 1) for v in f.bbox]}|{context}".encode()).hexdigest()[:8]
        raw = await cached(
            page.cache(model, f"f{i}-d{DESCRIBE_PROMPT_VERSION}-{what}"),
            lambda: ask(prompt, png, model, _semaphore("figure", FIGURE_CONCURRENCY)),
            "describe",
        )
    except Exception as e:
        log.warning("figure_not_described", extra={"figure": i + 1, "page": str(page), "error": str(e)})
        return f
    description = " ".join(str(raw.get("description") or "").split())
    if raw.get("useful") is False or not description:
        return f
    values = tuple(
        v
        for v in (
            " ".join(str(x).split()) for x in items(raw.get("key_values")) if isinstance(x, str | int | float)
        )
        if v
    )
    return replace(f, description=description, values=values, words=inside, png=png)


def _is_picture(t: SeenTable, words: list[Word]) -> bool:
    box = pad(t.bbox)
    inside = sum(1 for w in words if center_in(w, box))
    return t.style == "image" or inside < MIN_TEXT_WORDS


async def _with_transcript(page: PageRef, i: int, t: SeenTable, model: str) -> SeenTable:
    raw = await cached(
        page.cache(model, f"t{i}-v{TRANSCRIBE_PROMPT_VERSION}"),
        lambda: ask(TRANSCRIBE_PROMPT, lambda: page.render(pad(t.bbox)), model),
        "transcribe",
    )
    rows = [tuple(clean_cell(c) for c in row) for row in items(raw.get("rows")) if isinstance(row, list)]
    header = tuple(clean_cell(c) for c in items(raw.get("header")))
    return replace(t, transcript=(header, *rows))
