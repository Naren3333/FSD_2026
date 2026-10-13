"""Snapping a figure's box to its content.

The model's figure boxes are rough and often too tight: a chart's axis title or the ends of its labels fall
outside (CLIP's Figure 7 lost "KITTI" and its axis title, so its description named the axis "Distance"). Before a
figure is described, its box is snapped to its content (`snap`): the drawings and pictures
it mostly covers, the label lines its edges cut through, and the small label lines just outside it."""

import re
from pathlib import Path

import pymupdf

from ingest.geometry import FULL_PAGE_SHARE, Box, Line, area, center_in, intersection, pad, within
from ingest.pages import pictures
from ingest.strings import unligature

SNAP_GROW = 60.0  # a snapped figure box grows at most this much on each side


SNAP_COVER = 0.5  # a drawing or picture at least this much inside the (padded) box is part of the figure


SNAP_REACH = 1.5  # a label line this close (x its height) outside an edge is part of the figure...


SNAP_MAX_WORDS = 12  # ...if it is at most this many words...


SNAP_LABEL_RATIO = (
    0.8  # ...and a label: at most this size relative to the page's body text (captions are only
)


SNAP_SHORT_CHARS = (
    25  # a bit smaller), or at most this many characters and SNAP_SHORT_WORDS words; not a bullet,
)


SNAP_SHORT_WORDS = 3  # and nearer this figure than any other


SNAP_MARGIN = (
    0.07  # a grown box never takes in a line in the top or bottom share of the page (running headers),
)


SNAP_PROSE_CHARS = 40  # a caption, a bullet, or a line this long at near body size (running text)


CAPTION_START = re.compile(r"(figure|fig\.?|table|exhibit|chart)\s*[0-9A-Z]", re.IGNORECASE)


BULLET_START = "▪•◦‣●○■□►▸➢➤✓✔–"


def snap(
    box: Box, graphics: list[Box], lines: list[Line], blockers: list[Box], size: tuple[float, float]
) -> Box:
    """The figure's box grown to its content (see the module docstring), never by more than SNAP_GROW a side,
    never past the page, and never further into `blockers` (the page's tables and other figures)."""
    w, h = size
    limit = (
        max(0.0, box[0] - SNAP_GROW),
        max(0.0, box[1] - SNAP_GROW),
        min(w, box[2] + SNAP_GROW),
        min(h, box[3] + SNAP_GROW),
    )
    out = box
    body = _body_size(lines)
    text = [lb for lb, t, font in lines if _running_text(lb, t, font, body, h)]

    def grown(extra: Box) -> Box | None:
        b = (min(out[0], extra[0]), min(out[1], extra[1]), max(out[2], extra[2]), max(out[3], extra[3]))
        inside = limit[0] <= b[0] and limit[1] <= b[1] and b[2] <= limit[2] and b[3] <= limit[3]
        deeper = any(intersection(b, k) > intersection(out, k) for k in blockers)
        swallows = any(center_in(lb, b) and not center_in(lb, out) for lb in text)
        return b if inside and not deeper and not swallows else None

    for g in sorted(graphics, key=area, reverse=True):
        if area(g) < FULL_PAGE_SHARE * w * h and within(g, pad(box), SNAP_COVER):
            out = grown(g) or out
    for _ in range(2):  # tick labels first, then the axis title beyond them
        for lb, t, font in lines:
            small = font <= SNAP_LABEL_RATIO * body
            short = len(t) <= SNAP_SHORT_CHARS and len(t.split()) <= SNAP_SHORT_WORDS
            # at body size, a short line ending a sentence or going on in lower case is a paragraph's last line
            tail = t[-1] in ".?:)" or (t[0].islower() and len(t.split()) > 1)
            label = (small or (short and not tail)) and t[0] not in BULLET_START
            if within(lb, out, 1.0) or not label or len(t.split()) > SNAP_MAX_WORDS:
                continue
            dx, dy = _gaps(lb, out)
            reach = SNAP_REACH * (lb[3] - lb[1])
            near = (dx == 0 and dy <= reach) or (
                dy == 0 and dx <= reach
            )  # cut by an edge (0, 0), or just outside
            if near and all(max(_gaps(lb, k)) > max(dx, dy) for k in blockers):
                out = grown(lb) or out
    return out


def _running_text(lb: Box, text: str, font: float, body: float, height: float) -> bool:
    """A line no figure box may take in: a running header or footer, a caption, a bullet, or a long line at
    about body size."""
    return (
        lb[1] < SNAP_MARGIN * height
        or lb[3] > (1 - SNAP_MARGIN) * height
        or bool(CAPTION_START.match(text))
        or text[0] in BULLET_START
        or (len(text) > SNAP_PROSE_CHARS and font > SNAP_LABEL_RATIO * body)
    )


def _gaps(a: Box, b: Box) -> tuple[float, float]:
    """The horizontal and vertical distance between two boxes (0 where they overlap)."""
    return max(b[0] - a[2], a[0] - b[2], 0.0), max(b[1] - a[3], a[1] - b[3], 0.0)


def _body_size(lines: list[Line]) -> float:
    """The page's most common font size, weighted by characters."""
    sizes: dict[float, int] = {}
    for _, text, font in lines:
        sizes[round(font * 2) / 2] = sizes.get(round(font * 2) / 2, 0) + len(text)
    return max(sizes, key=lambda s: sizes[s]) if sizes else 0.0


def geometry(path: Path, number: int) -> tuple[list[Box], list[Line]]:
    """A page's drawings (clustered) and pictures, and its visible text lines, for `snap`."""
    with pymupdf.open(path) as doc:
        page = doc[number]
        return [tuple(r) for r in page.cluster_drawings()] + pictures(page), _lines(page)


def _lines(page: pymupdf.Page) -> list[Line]:
    """The page's visible text lines, in the PDF's order."""
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", ()):
            spans = [
                sp for sp in ln["spans"] if sp["alpha"] != 0
            ]  # whitespace-only spans too: some PDFs space so
            text = unligature(" ".join("".join(sp["text"] for sp in spans).split()))
            if text:
                lines.append((tuple(ln["bbox"]), text, max(sp["size"] for sp in spans if sp["text"].strip())))
    return lines
