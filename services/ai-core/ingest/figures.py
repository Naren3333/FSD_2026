"""Figures (PLAN.md §6.1 ingest step 2, RAG_PLAN Iteration 11). Blocking: runs while parsing.

The vision pass (ingest.vision) finds each page's figures and describes the informative ones from their
crop, with the PDF words inside them as grounding. Here they become `Figure`s: each is indexed as a chunk of its
own (kind "figure", source "vision"), and its crop is stored under a content-hash name so a reindex never
overwrites an image the live chunks still point at.

The words inside a figure (diagram labels) leave the prose, where they made stray headings and noise; they stay
searchable in the figure's embedded text. A figure covering most of the page (a whole slide drawn as a picture)
keeps its words in the prose too: there, the "figure" is the page.
"""

import hashlib
from dataclasses import dataclass, field

import pymupdf

from ingest.tables import Table
from ingest.vision import TABLE_OVERLAP, Box, SeenFigure, area, overlap

MAX_STRIP_SHARE = 0.6  # a figure covering at least this share of the page keeps its words in the prose


@dataclass(frozen=True)
class Figure:
    bbox: Box
    type: str
    description: str
    caption: str | None = None
    values: tuple[str, ...] = ()  # key values read off a chart, each marked approximate
    words: str = ""  # the PDF's own words inside it (labels), verbatim
    png: bytes = field(default=b"", repr=False)
    strip: bool = True  # its words leave the prose

    @property
    def image(self) -> str:
        """The crop's file name: its content hash, so the same crop always has the same name."""
        return image_name(self.png)


def image_name(png: bytes) -> str:
    return f"{hashlib.sha256(png).hexdigest()[:16]}.png"


def page_figures(page: pymupdf.Page, seen: list[SeenFigure], tables: list[Table]) -> list[Figure]:
    """The page's described figures, top to bottom; one that is a table found on the page is dropped."""
    page_area = area(tuple(page.rect))
    out = [
        Figure(
            f.bbox,
            f.type,
            f.description,
            f.caption,
            f.values,
            f.words,
            f.png,
            area(f.bbox) < MAX_STRIP_SHARE * page_area,
        )
        for f in seen
        if f.description and f.png and not any(overlap(f.bbox, t.bbox) >= TABLE_OVERLAP for t in tables)
    ]
    return sorted(out, key=lambda f: f.bbox[1])
