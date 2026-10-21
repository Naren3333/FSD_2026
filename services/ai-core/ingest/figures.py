"""attach native labels and crops to informative figure descriptions."""

import hashlib
from dataclasses import dataclass, field

import pymupdf

from ingest.geometry import TABLE_OVERLAP, Box, area, overlap
from ingest.tables import Table
from ingest.vision import SeenFigure

MAX_STRIP_SHARE = 0.6  # a figure covering at least this share of the page keeps its words in the prose


@dataclass(frozen=True)
class Figure:
    bbox: Box
    type: str
    description: str
    caption: str | None = None
    values: tuple[str, ...] = ()  # key values read off a chart, each marked approximate
    words: str = ""  # the pdf's own words inside it (labels), verbatim
    png: bytes = field(default=b"", repr=False)
    strip: bool = True  # its words leave the prose

    @property
    def image(self) -> str:
        """the crop's file name: its content hash, so the same crop always has the same name."""
        return image_name(self.png)


def image_name(png: bytes) -> str:
    return f"{hashlib.sha256(png).hexdigest()[:16]}.png"


def page_figures(page: pymupdf.Page, seen: list[SeenFigure], tables: list[Table]) -> list[Figure]:
    """the page's described figures, top to bottom; one that is a table found on the page is dropped."""
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
