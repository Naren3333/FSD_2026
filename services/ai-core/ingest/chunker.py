"""The structure-aware chunker: paragraphs packed to at most MAX_TOKENS
with a little overlap, never across a top-level section.

A chunk's section_path is the common prefix of the paths of the body paragraphs new to it (the overlap
carried from the previous chunk and the headings themselves don't count). Headings stay in the content but never end a chunk.

A table is a chunk of its own (kind "table"): its caption, then the table as Markdown. One over MAX_TOKENS
is split by rows, the header repeated in every piece. What gets embedded (`embed_body`) is the caption
and one sentence per row with each cell labelled by its column ("Criterion: Analysis; Band 4: ..."), so a
question about one row matches it. Text never packs across a table, and no overlap is carried over one.

Text transcribed from a scanned page (OCR) is packed like any other; a chunk touching such a page is
source "vision".

A described figure is a chunk of its own too (kind "figure", source "vision"), after the text around it; unlike
a table it doesn't split that text (a diagram mid-slide would cut the slide's bullets in two): its caption, a "[diagram,
described by AI]" line and the description, plus any key values. Its embedded text adds the PDF's own words
inside the figure (the diagram labels taken out of the prose), so keyword search still finds them. The chunk
points at the stored crop by name (`image`).

Tables and figures with a numbered caption carry its label ("figure 7", "table 2"), and text chunks the labels
they mention (`cites`), so search can hand back the figure a paragraph is talking about.
Blocking (tiktoken): run it in a thread.
"""

import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import cache
from itertools import zip_longest
from pathlib import Path

import tiktoken
from ingest.headings import Para
from ingest.tables import markdown_lines

MAX_TOKENS = 600
OVERLAP_TOKENS = 90  # ~15% of MAX_TOKENS: whole trailing paragraphs, or the end of the last one
SEPARATOR = "\n\n"
PATH_SEPARATOR = " > "
WINDOW_SLACK = 10  # re-encoding a decoded window can come out a token or two longer
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")
CONTINUED = "(cont.)"
DESCRIBED = "described by AI"
MAX_LABEL_TOKENS = 300  # a figure's own words, as embedded
MIN_ROW_TOKENS = 100  # room for rows in a table chunk, even when the caption and header alone fill MAX_TOKENS
TRANSCRIBED = "(transcribed from an image)"
STUB_WORDS = 5  # a text chunk shorter than this is dropped when another chunk covers its page
LABEL = re.compile(r"\b(fig(?:ure)?s?\.?|tables?|exhibits?)\s*(\d+[a-z]?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    content: str
    page_start: int
    page_end: int
    token_count: int
    section_path: str | None = None
    kind: str = "text"  # "text" | "table" | "figure"
    source: str = "pdf"  # "pdf": the PDF's own text; "vision": written by the vision model
    embed_body: str | None = None  # embedded instead of content when set (row sentences; a figure's labels)
    image: str | None = None  # a figure's crop file name
    label: str | None = None  # a table's or figure's numbered caption label: "figure 7", "table 2"
    cites: tuple[str, ...] = ()  # the labels a text chunk mentions


def label_of(caption: str | None) -> str | None:
    """The label a caption opens with ("Fig. 7. ..." → "figure 7"), or None."""
    m = LABEL.match(caption.strip()) if caption else None
    return _label(m) if m else None


def cited(text: str) -> tuple[str, ...]:
    """The labels a text mentions, each once, in order."""
    return tuple(dict.fromkeys(_label(m) for m in LABEL.finditer(text)))


def _label(m: re.Match) -> str:
    word = m.group(1).lower()
    kind = "figure" if word.startswith("fig") else "table" if word.startswith("table") else "exhibit"
    return f"{kind} {m.group(2).lower()}"


@dataclass(frozen=True)
class _Unit:
    page: int
    text: str
    cost: int  # tokens + 1 for the separator
    path: tuple[str, ...] = ()
    heading: bool = False


# cl100k_base's BPE file is committed beside the service (OpenAI's published file, checked by tiktoken against
# its own sha256), so ingest and the tests never download it.
TIKTOKEN_DIR = Path(__file__).resolve().parents[1] / "tiktoken"


@cache
def encoding() -> tiktoken.Encoding:
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(TIKTOKEN_DIR))
    return tiktoken.get_encoding("cl100k_base")


_limits: ContextVar[tuple[int, int]] = ContextVar("chunk_limits", default=(MAX_TOKENS, OVERLAP_TOKENS))


def chunk_paras(
    paras: list[Para],
    vision_pages: frozenset[int] = frozenset(),
    *,
    max_tokens: int = MAX_TOKENS,
    overlap: int = OVERLAP_TOKENS,
    page_boundaries: bool = False,
) -> list[Chunk]:
    if not 200 <= max_tokens <= 2000 or not 0 <= overlap < max_tokens // 2:
        raise ValueError("CHUNK_TOKENS must be 200-2000 and CHUNK_OVERLAP less than half of it")
    token = _limits.set((max_tokens, overlap))
    try:
        if not page_boundaries:
            return _chunk_paras(paras, vision_pages)
        pages: dict[int, list[Para]] = {}
        for para in paras:
            pages.setdefault(para.page, []).append(para)
        chunks = [c for page in pages.values() for c in _chunk_paras(page, vision_pages)]
        return [replace(c, ordinal=i) for i, c in enumerate(chunks)]
    finally:
        _limits.reset(token)


def _chunk_paras(paras: list[Para], vision_pages: frozenset[int]) -> list[Chunk]:
    """`vision_pages`: pages whose text the vision model transcribed (OCR); a text chunk on one is source "vision"."""
    enc = encoding()
    chunks: list[Chunk] = []
    for section in _sections(paras):
        run: list[Para] = []
        figures: list[
            Para
        ] = []  # a figure doesn't break the text around it (a slide's bullets stay together)
        for p in section:
            if p.figure is not None:
                figures.append(p)
            elif p.table is None:
                run.append(p)
            else:
                chunks += _text_chunks(enc, run) + [c for f in figures for c in _figure_chunks(enc, f)]
                chunks += _table_chunks(enc, p)
                run, figures = [], []
        chunks += _text_chunks(enc, run) + [c for f in figures for c in _figure_chunks(enc, f)]
    chunks = _without_stubs(chunks)
    return [
        replace(c, ordinal=i, source="vision" if c.kind == "text" and _touches(c, vision_pages) else c.source)
        for i, c in enumerate(chunks)
    ]


def _without_stubs(chunks: list[Chunk]) -> list[Chunk]:
    """Drop text chunks of a few words, such as a slide title whose body became a table or figure chunk, or a
    "Continued on next page" line, when another chunk covers the same page: alone they only take search slots."""

    def stub(c: Chunk) -> bool:
        return c.kind == "text" and len(c.content.split()) < STUB_WORDS

    def covered(c: Chunk) -> bool:
        return any(
            o is not c and not stub(o) and o.page_start <= c.page_end and c.page_start <= o.page_end
            for o in chunks
        )

    return [c for c in chunks if not (stub(c) and covered(c))]


def _touches(chunk: Chunk, pages: frozenset[int]) -> bool:
    return any(chunk.page_start <= p <= chunk.page_end for p in pages)


def _text_chunks(enc: tiktoken.Encoding, paras: list[Para]) -> list[Chunk]:
    chunks = []
    for group, carried in _pack(enc, [u for p in paras for u in _units(enc, p)]):
        content = SEPARATOR.join(u.text for u in group)
        new = group[carried:]
        labels = [
            u for u in new if not u.heading
        ] or new  # a heading introduces its section; its parents don't count
        # text under no heading (a title page joined to the first section) doesn't wipe out the path
        path = PATH_SEPARATOR.join(_common_prefix([u.path for u in labels if u.path])) or None
        chunks.append(
            Chunk(
                0,
                content,
                group[0].page,
                group[-1].page,
                len(enc.encode(content, disallowed_special=())),
                path,
                cites=cited(content),
            )
        )
    return chunks


def _table_chunks(enc: tiktoken.Encoding, para: Para) -> list[Chunk]:
    """The table as Markdown under its caption, split by rows (header repeated) to fit MAX_TOKENS."""
    table = para.table
    assert table is not None
    caption = " ".join(x for x in (table.caption, TRANSCRIBED if table.source == "vision" else None) if x)
    lines = markdown_lines(table)
    head, row_lines = lines[:2], lines[2:]
    # A very wide header leaves no room under MAX_TOKENS: its chunks then run over it rather than lose rows.
    budget = max(
        MIN_ROW_TOKENS,
        _limits.get()[0]
        - WINDOW_SLACK
        - _tokens(enc, f"{caption} {CONTINUED}")
        - sum(_tokens(enc, h) + 1 for h in head),
    )
    rows: list[tuple[tuple[str, ...], str]] = []
    for row, line in zip(table.rows, row_lines, strict=False):
        ids = enc.encode(line, disallowed_special=())
        if len(ids) <= budget:
            rows.append((row, line))
        else:  # rare: one row longer than a chunk, cut into token windows
            rows += [(row, enc.decode(ids[i : i + budget])) for i in range(0, len(ids), budget)]
    groups: list[list[tuple[tuple[str, ...], str]]] = [[]]
    used = 0
    for row, line in rows:
        cost = _tokens(enc, line) + 1
        if groups[-1] and used + cost > budget:
            groups.append([])
            used = 0
        groups[-1].append((row, line))
        used += cost
    path = PATH_SEPARATOR.join(para.path) or None
    chunks = []
    for i, group in enumerate(groups):
        title = " ".join(x for x in (caption, CONTINUED if i else None) if x)
        content = SEPARATOR.join(x for x in (title, "\n".join(head + [line for _, line in group])) if x)
        sentences = list(dict.fromkeys(_row_sentence(table.header, row) for row, _ in group))
        body = SEPARATOR.join(x for x in (title, "\n".join(s for s in sentences if s)) if x)
        chunks.append(
            Chunk(
                0,
                content,
                para.page,
                table.last_page or para.page,
                _tokens(enc, content),
                path,
                "table",
                table.source,
                body,
                label=label_of(table.caption),
            )
        )
    return chunks


def _figure_chunks(enc: tiktoken.Encoding, para: Para) -> list[Chunk]:
    """The figure's caption, an "[<type>, described by AI]" line, the description and its key values."""
    figure = para.figure
    assert figure is not None
    head = SEPARATOR.join(x for x in (figure.caption, f"[{figure.type or 'figure'}, {DESCRIBED}]") if x)
    values = f"Key values (approximate): {'; '.join(figure.values)}" if figure.values else ""
    budget = _limits.get()[0] - WINDOW_SLACK - _tokens(enc, head)
    body = _clip(enc, "\n".join(x for x in (figure.description, values) if x), budget)
    content = f"{head}\n{body}"
    labels = _clip(enc, f"Labels: {figure.words}", MAX_LABEL_TOKENS) if figure.words.strip() else ""
    embed = SEPARATOR.join(x for x in (content, labels) if x)
    path = PATH_SEPARATOR.join(para.path) or None
    return [
        Chunk(
            0,
            content,
            para.page,
            para.page,
            _tokens(enc, content),
            path,
            "figure",
            "vision",
            embed,
            figure.image,
            label_of(figure.caption),
        )
    ]


def _clip(enc: tiktoken.Encoding, text: str, budget: int) -> str:
    ids = enc.encode(text, disallowed_special=())
    return text if len(ids) <= budget else enc.decode(ids[:budget]).rstrip() + " …"


def _row_sentence(header: tuple[str, ...] | None, row: tuple[str, ...]) -> str:
    """One row as text for embedding, each cell labelled by its column."""
    if header and any(header):
        return "; ".join(f"{h}: {v}" if h else v for h, v in zip_longest(header, row, fillvalue="") if v)
    return " | ".join(c for c in row if c)


def _tokens(enc: tiktoken.Encoding, text: str) -> int:
    return len(enc.encode(text, disallowed_special=()))


def _sections(paras: list[Para]) -> list[list[Para]]:
    """Split before every top-level heading (the smallest level the document has), except the first:
    text before it (a title page) joins the first section instead of becoming a tiny chunk of its own.
    When the headings come from the PDF outline, before second-level ones too: an outline's top level is often
    a few parts ("Part 1", "Part 2"), and splitting only there would pack unrelated sections together."""
    levels = [p.level for p in paras if p.level is not None]
    top = min(levels, default=None)
    split = None if top is None else top + (1 if any(p.toc for p in paras) else 0)
    sections: list[list[Para]] = [[]]
    seen_top = False
    for p in paras:
        if p.level is not None and split is not None and p.level <= split:
            # a second-level heading right under its parent (no text between) stays in the parent's section
            if seen_top and (p.level == top or any(q.level is None for q in sections[-1])):
                sections.append([])
            seen_top = True
        sections[-1].append(p)
    return [s for s in sections if s]


def _pack(enc: tiktoken.Encoding, units: list[_Unit]) -> list[tuple[list[_Unit], int]]:
    """Greedy packing; each group comes with how many of its leading units are carried overlap."""
    groups: list[tuple[list[_Unit], int]] = []
    current: list[_Unit] = []
    carried = 0
    for unit in units:
        if len(current) > carried and _cost(current) + unit.cost > _limits.get()[0]:
            moved: list[_Unit] = []  # a heading at the end of a chunk belongs with what follows it
            while len(current) > carried + 1 and current[-1].heading:
                moved.insert(0, current.pop())
            if (
                _cost(moved) + unit.cost > _limits.get()[0]
            ):  # rare: the heading would push the next chunk over
                current += moved
                moved = []
            groups.append((current, carried))
            current = _tail(enc, current)
            if _cost(current) + _cost(moved) + unit.cost > _limits.get()[0]:
                current = []
            carried = len(current)
            current += moved
        current.append(unit)
    if len(current) > carried:
        groups.append((current, carried))
    return groups


def _units(enc: tiktoken.Encoding, para: Para) -> list[_Unit]:
    """One unit per paragraph. One over MAX_TOKENS is split into sentence runs that fit, and a single
    sentence over it into token windows."""
    tokens = len(enc.encode(para.text, disallowed_special=()))
    if tokens < _limits.get()[0]:
        return [_Unit(para.page, para.text, tokens + 1, para.path, para.level is not None)]
    size = _limits.get()[0] - WINDOW_SLACK
    pieces: list[str] = []
    run: list[str] = []
    for sentence in SENTENCE_END.split(para.text):
        if len(enc.encode(" ".join([*run, sentence]), disallowed_special=())) <= size:
            run.append(sentence)
            continue
        if run:
            pieces.append(" ".join(run))
        run = [sentence]
        if len(enc.encode(sentence, disallowed_special=())) > size:
            ids = enc.encode(sentence, disallowed_special=())
            pieces += [enc.decode(ids[i : i + size]).strip() for i in range(0, len(ids), size)]
            run = []
    if run:
        pieces.append(" ".join(run))
    pieces = [p.strip() for p in pieces]
    return [
        _Unit(para.page, p, len(enc.encode(p, disallowed_special=())) + 1, para.path) for p in pieces if p
    ]


def _cost(units: list[_Unit]) -> int:
    return sum(u.cost for u in units)


def _common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    prefix = paths[0] if paths else ()
    for path in paths[1:]:
        n = 0
        while n < min(len(prefix), len(path)) and prefix[n] == path[n]:
            n += 1
        prefix = prefix[:n]
    return prefix


def _tail(enc: tiktoken.Encoding, units: list[_Unit]) -> list[_Unit]:
    """The trailing units that fit in OVERLAP_TOKENS. If the last one alone is bigger (common: a
    paragraph is often ~100 tokens), its last _limits.get()[1] instead, from a sentence or word start."""
    if _limits.get()[1] == 0:
        return []
    tail: list[_Unit] = []
    for unit in reversed(units):
        if _cost(tail) + unit.cost > _limits.get()[1]:
            break
        tail.insert(0, unit)
    if tail or not units:
        return tail
    last = units[-1]
    piece = enc.decode(enc.encode(last.text, disallowed_special=())[-(_limits.get()[1] - 1) :])
    sentence = piece.find(". ")
    piece = piece[sentence + 2 :] if 0 <= sentence < len(piece) // 2 else piece.partition(" ")[2]
    piece = piece.strip()
    return (
        [_Unit(last.page, piece, len(enc.encode(piece, disallowed_special=())) + 1, last.path)]
        if piece
        else []
    )
