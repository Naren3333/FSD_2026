"""Plain text and Markdown → paragraphs, headings and tables for the chunker. Blocking only in the trivial sense:
it is pure string work.

A text file has no pages, so everything is on page 1 and the document reports one page; chunks are located by
their section path instead.

Markdown keeps the structure the author wrote, so nothing is guessed from font sizes: ATX (`## Heading`) and
setext (a line underlined with `===` or `---`) headings set the section path, their levels ranked as in a PDF (the
biggest heading used is level 0, except a lone title above all the others, which is text as a PDF's cover title is); GitHub pipe tables become tables (chunked by rows with the header repeated, like
a PDF's); a fenced code block is one paragraph, never split into headings; YAML front matter is left out. Every
other run of lines up to a blank line is a paragraph, its line breaks kept so lists stay lists.

Plain text has no markup to trust (a `#` line is as likely a shell comment as a heading), so it is paragraphs only.
"""

import re

from ingest.chunker import SENTENCE_END
from ingest.headings import MAX_CONTEXT_CHARS, MAX_LEVELS, Para
from ingest.strings import clean_cell
from ingest.tables import Table, markdown

ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
TABLE_RULE = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
THEMATIC_BREAK = re.compile(r"^ {0,3}([-*_])([ \t]*\1){2,}[ \t]*$")
NO_BOX = (0.0, 0.0, 0.0, 0.0)

Item = tuple[int, str] | str | Table  # (raw heading level 1-6, text), a paragraph, or a table


def paragraphs(text: str, markup: bool) -> list[Para]:
    """The document's paragraphs in order. `markup`: read it as Markdown, not plain text."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    items = _markdown_items(lines) if markup else _plain_items(lines)
    headings = [i for i in items if isinstance(i, tuple)]
    if len(headings) > 1 and headings[0][0] < min(h[0] for h in headings[1:]):
        # a title above every other heading (one "# Title", then "##" sections) is the document's name, as a PDF's
        # cover title is: its sections are the top level, so chunks break between them
        items[items.index(headings[0])] = headings[0][1]
    ranked = sorted({i[0] for i in items if isinstance(i, tuple)})
    level_of = {raw: min(n, MAX_LEVELS - 1) for n, raw in enumerate(ranked)}
    paras: list[Para] = []
    stack: list[tuple[int, str]] = []
    for item in items:
        path = tuple(t for _, t in stack)
        if isinstance(item, tuple):
            level = level_of[item[0]]
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, item[1]))
            paras.append(Para(item[1], 1, tuple(t for _, t in stack), level))
        elif isinstance(item, Table):
            paras.append(_table_para(item, path, paras))
        else:
            paras.append(Para(item, 1, path))
    return paras


def _table_para(table: Table, path: tuple[str, ...], paras: list[Para]) -> Para:
    """The table, captioned (as in a PDF) by the last sentence of the paragraph just before it, which usually
    introduces it."""
    if paras and paras[-1].level is None and not paras[-1].region:
        context = SENTENCE_END.split(paras[-1].text.strip())[-1]
        if context and len(context) <= MAX_CONTEXT_CHARS:
            table = Table(table.bbox, table.header, table.rows, context)
    return Para(markdown(table), 1, path, None, table)


def _plain_items(lines: list[str]) -> list[Item]:
    items: list[Item] = []
    block: list[str] = []
    for line in [*lines, ""]:
        if line.strip():
            block.append(line.rstrip())
        elif block:
            items.append("\n".join(block))
            block = []
    return items


def _markdown_items(lines: list[str]) -> list[Item]:
    items: list[Item] = []
    block: list[str] = []

    def flush() -> None:
        if block:
            items.append("\n".join(block))
            block.clear()

    i = _after_front_matter(lines)
    while i < len(lines):
        line = lines[i].rstrip()
        if fence := FENCE.match(line):
            flush()
            mark = fence.group(1)
            end = next(
                (j for j in range(i + 1, len(lines)) if lines[j].strip().startswith(mark[0] * len(mark))),
                len(lines),
            )
            code = "\n".join(x.rstrip() for x in lines[i : end + 1])
            if code.strip():
                items.append(code)
            i = end + 1
            continue
        if heading := ATX.match(line):
            flush()
            if text := (heading.group(2) or "").strip():
                items.append((len(heading.group(1)), text))
            i += 1
            continue
        if block and len(block) == 1 and (setext := SETEXT.match(line)) and not _listed(block[0]):
            items.append((1 if setext.group(1)[0] == "=" else 2, block[0].strip()))
            block.clear()
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()
            header = _cells(line)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_cells(lines[i]))
                i += 1
            items.append(Table(NO_BOX, header, tuple(rows)))
            continue
        if not line.strip() or THEMATIC_BREAK.match(line):
            flush()
        else:
            block.append(line)
        i += 1
    flush()
    return items


def _after_front_matter(lines: list[str]) -> int:
    """The first line after a YAML front matter block (`---` ... `---`), or 0 without one."""
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                return j + 1
    return 0


def _listed(line: str) -> bool:
    """A list item: a setext underline below it is a thematic break, not a heading."""
    return bool(re.match(r"^ {0,3}([-*+]|\d{1,9}[.)])[ \t]", line))


def _cells(line: str) -> tuple[str, ...]:
    """A pipe-table row's cells; an escaped `\\|` stays in its cell."""
    row = line.strip()
    row = row.removeprefix("|")
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return tuple(clean_cell(c.replace("\\|", "|")) for c in re.split(r"(?<!\\)\|", row))
