"""small text helpers shared by the parser and the table extractor."""

import re

HYPHEN_BREAK = re.compile(r"[a-z]-$")  # "repre-" at a line end, continued by "sentations"
# expand ligatures without flattening superscripts or subscripts.
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})


def unligature(text: str) -> str:
    return text.translate(LIGATURES)


def join_lines(first: str, second: str) -> str:
    """join two lines of a paragraph, undoing a hyphenated line break between lower-case letters."""
    if HYPHEN_BREAK.search(first) and second[:1].islower():
        return first[:-1] + second
    return f"{first} {second}"


def clean_cell(cell: object) -> str:
    """a table cell's text: whitespace collapsed, its lines joined (none, a merged cell, becomes \"\")."""
    if cell is None:
        return ""
    text = ""
    for raw in unligature(str(cell)).splitlines():
        line = " ".join(raw.split())
        if line:
            text = join_lines(text, line) if text else line
    return text
