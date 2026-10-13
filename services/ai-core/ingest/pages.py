"""What a PDF page shows, read with PyMuPDF: its visible words, pictures, a content key for caching, and renders.

The page's key (`page_key`) hashes what the page shows, not its bytes: its size, words, pictures and drawings. A
page without a text layer (a scan: under MIN_PAGE_CHARS of visible PDF text) is transcribed by the vision pass.
Hidden text (a scanner's invisible OCR layer, or fully transparent text) doesn't count."""

import hashlib
from pathlib import Path

import pymupdf

from ingest.geometry import Box, Word, area, center_in, intersection
from ingest.strings import unligature

DPI = 110


CROP_DPI = 150


MIN_PAGE_CHARS = 50  # a page with less PDF text than this has no text layer: it is transcribed (OCR)


TILE_GAP = 2.0  # image tiles this close are one picture (scanners often write a page as strips)


TILE_OVERLAP = 0.1  # ...unless they lie on each other by more than this share of the smaller one


def page_key(page: pymupdf.Page) -> str:
    """What the page shows, hashed: its size, words with their boxes, embedded pictures (by content digest) and
    drawings. The same slide in a revised deck has the same key; a changed word, picture or shape gives a new one.
    Not a hash of the rendered pixels, which would change with the renderer's version; and the raw PDF words, not
    the cleaned-up ones, so a change to the cleaning never re-keys (and so re-samples) the cache."""
    h = hashlib.sha256()
    h.update(repr((_r(page.rect.width), _r(page.rect.height))).encode())
    for w in page.get_text("words", sort=True):
        h.update(repr((*map(_r, w[:4]), w[4])).encode())
    for i in page.get_image_info(hashes=True):
        h.update(repr((bytes(i.get("digest") or b"").hex(), *map(_r, i["bbox"]))).encode())
    for d in page.get_drawings():
        h.update(repr((d.get("type"), *map(_r, d["rect"]), d.get("fill"), d.get("color"))).encode())
    return h.hexdigest()


def _r(v: float) -> float:
    return round(v * 2) / 2  # 0.5pt


def visible_words(page: pymupdf.Page) -> list[Word]:
    """The page's words, minus hidden text (alpha 0: a scanner's invisible OCR layer, or transparent text)."""
    words = [(*w[:4], unligature(w[4])) for w in page.get_text("words", sort=True)]
    spans = [
        s for b in page.get_text("dict")["blocks"] if b["type"] == 0 for ln in b["lines"] for s in ln["spans"]
    ]
    hidden = [tuple(s["bbox"]) for s in spans if s["alpha"] == 0 and s["text"].strip()]
    if not hidden:
        return words
    shown = [tuple(s["bbox"]) for s in spans if s["alpha"] != 0 and s["text"].strip()]
    return [
        w for w in words if not any(center_in(w, h) for h in hidden) or any(center_in(w, s) for s in shown)
    ]


def pictures(page: pymupdf.Page) -> list[Box]:
    """The page's images, tiles merged; top to bottom. Tiles touch (within TILE_GAP) without lying on each other:
    a picture placed over a page-sized background stays its own picture."""
    boxes = [
        b for b in (tuple(pymupdf.Rect(i["bbox"]) & page.rect) for i in page.get_image_info()) if area(b) > 0
    ]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if _tiles(boxes[i], boxes[j]):
                    a, b = boxes[i], boxes.pop(j)
                    boxes[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    merged = True
                    break
            if merged:
                break
    return sorted(boxes, key=lambda b: (b[1], b[0]))


def _tiles(a: Box, b: Box) -> bool:
    near = (
        area(
            (
                max(a[0], b[0]) - TILE_GAP,
                max(a[1], b[1]) - TILE_GAP,
                min(a[2], b[2]) + TILE_GAP,
                min(a[3], b[3]) + TILE_GAP,
            )
        )
        > 0
    )
    return near and intersection(a, b) < TILE_OVERLAP * min(area(a), area(b))


def _render(path: Path, number: int, clip: Box | None = None, sharp: bool = False) -> bytes:
    """The page (at DPI, or CROP_DPI when `sharp`: a transcript needs small print legible), or a crop of it."""
    with pymupdf.open(path) as doc:
        page = doc[number]
        if clip is None:
            return page.get_pixmap(dpi=CROP_DPI if sharp else DPI).tobytes("png")
        return page.get_pixmap(dpi=CROP_DPI, clip=pymupdf.Rect(clip) & page.rect).tobytes("png")


def has_text_layer(words: list[Word]) -> bool:
    return sum(len(w[4]) for w in words) >= MIN_PAGE_CHARS
