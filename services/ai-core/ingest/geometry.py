"""boxes on a pdf page, in points with a top-left origin: padding, area, overlap and containment."""

from collections.abc import Sequence
from typing import Any

FULL_PAGE_SHARE = (
    0.6  # a box covering this share of its page is the page itself: a scan, or a slide background
)

Box = tuple[float, float, float, float]


BOX_PAD = 6.0  # the model's boxes are rough: crop and search a little outside them


TABLE_OVERLAP = 0.6  # a figure this much the same region as a table the model found is that table


def pad(b: Box, d: float = BOX_PAD) -> Box:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def overlap(a: Box, b: Box) -> float:
    """how much two boxes are the same region: the intersection over the smaller one, discounted when one is much bigger than the other (a grid swallowing the page, or a sliver of a big box)."""
    inter = area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))
    small, big = sorted((area(a), area(b)))
    if not small:
        return 0.0
    return inter / small * min(1.0, 2.5 * small / big)


def within(a: Box, region: Box, share: float = TABLE_OVERLAP) -> bool:
    """at least `share` of box `a` lies inside `region`."""
    inter = area((max(a[0], region[0]), max(a[1], region[1]), min(a[2], region[2]), min(a[3], region[3])))
    return area(a) > 0 and inter / area(a) >= share


def center_in(w: Sequence[Any], box: Box) -> bool:
    return box[0] <= (w[0] + w[2]) / 2 <= box[2] and box[1] <= (w[1] + w[3]) / 2 <= box[3]


Word = tuple[float, float, float, float, str]


def intersection(a: Box, b: Box) -> float:
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


Line = tuple[Box, str, float]  # a text line: box, text, font size
