"""local, conservative selection of pages that need paid layout/vision help."""

from pathlib import Path

import pymupdf

from ingest.pages import pictures, visible_words


def select_pages(path: Path, limit: int) -> set[int]:
    """clean prose and sparse text-only title pages need no ocr."""
    selected: set[int] = set()
    with pymupdf.open(path) as doc:
        for n in range(min(limit, len(doc))):
            page = doc[n]
            words = visible_words(page)
            text = " ".join(w[4] for w in words)
            page_area = page.rect.width * page.rect.height
            images = pictures(page)
            graphics = page.get_drawings()
            # a few sizeable rectangles/connecting lines often encode a diagram even on a text-rich page.
            meaningful = [g for g in graphics if g["rect"].width > 15 or g["rect"].height > 15]
            has_image = any((b[2] - b[0]) * (b[3] - b[1]) > page_area * 0.015 for b in images)
            broken = text.count("\ufffd") > max(2, len(text) // 100)
            lines: dict[int, list[tuple[float, float]]] = {}
            for x0, y0, x1, _y1, _word in words:
                lines.setdefault(round(y0 / 4), []).append((x0, x1))
            # repeated, widely spaced cells are a conservative signal for an unruled table.
            tabular = (
                sum(
                    sum(b[0] - a[1] > 30 for a, b in zip(sorted(xs), sorted(xs)[1:], strict=False)) >= 2
                    for xs in lines.values()
                )
                >= 3
            )
            # a locally readable ruled table needs no model layout pass.
            found = page.find_tables(strategy="lines").tables if len(meaningful) >= 3 else []
            boxes = [pymupdf.Rect(t.bbox) for t in found if t.row_count >= 2 and t.col_count >= 2]
            unexplained = [g for g in meaningful if not any(box.contains(g["rect"]) for box in boxes)]
            if has_image or len(unexplained) >= 3 or broken or (tabular and not boxes):
                selected.add(n)
    return selected
