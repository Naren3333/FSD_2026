"""the document's own sentences that cite a figure."""

import re
from collections.abc import Sequence

import pymupdf

from ingest.snap import SNAP_LABEL_RATIO, _body_size, _lines

FIGURE_LABEL = re.compile(r"(?:figure|fig\.?|exhibit|chart)\s*(\d+[a-z]?)\b", re.IGNORECASE)


CONTEXT_SENTENCES = 3  # citing sentences passed with a figure...


CONTEXT_WORDS = 120  # ...at most this many words in all


CONTEXT_REACH = 40  # words taken each way from a citation, at most


ABBREVIATION = re.compile(r"\(?(figs?|e\.g|i\.e|al|cf|vs|resp|eq|sec)\.", re.IGNORECASE)  # not a sentence end


def page_text(page: pymupdf.Page) -> list[str]:
    """the page's running text, line by line: lines smaller than the page's body text (chart and diagram labels) are left out, so they don't run into the sentences around them."""
    lines = _lines(page)
    body = _body_size(lines)
    return [t for _, t, font in lines if font > SNAP_LABEL_RATIO * body]


def figure_context(pages: Sequence[Sequence[str]], caption: str | None, page: int) -> str:
    """collect nearby sentences referencing a numbered figure within the context limits."""
    label = FIGURE_LABEL.match(caption.strip()) if caption else None
    if not label:
        return ""
    cites = re.compile(
        rf"\(?(?:figures?|figs?\.?)\s*{re.escape(label.group(1))}(?![0-9a-z])(.?)", re.IGNORECASE
    )
    out: list[str] = []
    used = 0
    for n in sorted(range(len(pages)), key=lambda n: (abs(n - page), n)):
        words: list[str] = []
        starts: set[int] = set()
        for line in pages[n]:
            starts.add(len(words))
            words += line.split()
        for i in range(len(words)):
            m = cites.match(" ".join(words[i : i + 2]))
            if not m or (i in starts and m.group(1) in (".", ":")):
                continue
            j = i
            while j > 0 and i - j < CONTEXT_REACH and not _ends_sentence(words[j - 1]):
                j -= 1
            k = i + 1
            while k < len(words) - 1 and k - i < CONTEXT_REACH and not _ends_sentence(words[k]):
                k += 1
            sentence = " ".join(words[j : k + 1])
            if sentence in out:
                continue
            if used + k + 1 - j > CONTEXT_WORDS:
                return " ".join(out)
            out.append(sentence)
            used += k + 1 - j
            if len(out) == CONTEXT_SENTENCES:
                return " ".join(out)
    return " ".join(out)


def _ends_sentence(word: str) -> bool:
    return word[-1] in ".!?" and not ABBREVIATION.fullmatch(word)
