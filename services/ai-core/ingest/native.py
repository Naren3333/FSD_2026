"""Local structure readers. No Office execution, external relationships or model calls.

PDF-compatible numeric anchors mean slides for PPTX and logical sections for Markdown/DOCX,
never rendered pages for unpaginated files. Consumers must use `source` when displaying them.
"""

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from ingest.headings import Para
from ingest.parse import IngestError
from ingest.tables import Table, markdown

MARKDOWN = "text/markdown"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FORMATS = {MARKDOWN: "markdown", PPTX: "pptx", DOCX: "docx"}
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
MAX_EXPANDED = 64 * 1024 * 1024
MAX_XML = 8 * 1024 * 1024
MAX_MEMBERS = 4096


def tag(name: str) -> str:
    prefix, local = name.split(":")
    return "{" + NS[prefix] + "}" + local


@dataclass
class Structure:
    paras: list[Para] = field(default_factory=list)
    stack: list[tuple[int, str]] = field(default_factory=list)
    unit: int = 1
    characters: int = 0
    has_body: bool = False

    def check_size(self, text: str) -> None:
        self.characters += len(text)
        if self.characters > MAX_XML:
            raise IngestError("TOO_LARGE", "Extracted text exceeds size limit")

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(text for _, text in self.stack)

    def heading(self, text: str, level: int, *, slide: int | None = None) -> None:
        self.check_size(text)
        if slide is not None:
            self.unit = slide
        elif self.has_body:
            self.unit += 1
        self.has_body = False
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, text))
        if len(" > ".join(self.path)) > 20000:
            raise IngestError("TOO_LARGE", "Section path is too long")
        self.paras.append(Para(text, self.unit, self.path, level, toc=True))

    def body(self, text: str) -> None:
        if text.strip():
            self.check_size(text)
            self.paras.append(Para(text, self.unit, self.path))
            self.has_body = True

    def table(self, rows: list[tuple[str, ...]], header: bool = False) -> None:
        if not rows:
            return
        table = Table((0, 0, 1, 1), rows[0] if header else None, tuple(rows[1:] if header else rows))
        text = markdown(table)
        self.check_size(text)
        self.paras.append(Para(text, self.unit, self.path, table=table))
        self.has_body = True


class Package:
    def __init__(self, path: Path):
        self.zip = ZipFile(path)
        members = self.zip.infolist()
        if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > MAX_EXPANDED:
            self.zip.close()
            raise IngestError("TOO_LARGE", "Office package exceeds expanded-size or entry limit")
        if len({m.filename for m in members}) != len(members):
            self.zip.close()
            raise IngestError("CORRUPT", "Duplicate Office package entries")

    def xml(self, name: str) -> ET.Element:
        info = self.zip.getinfo(name)
        if info.file_size > MAX_XML:
            raise IngestError("TOO_LARGE", "Office XML part exceeds size limit")
        raw = self.zip.read(info)
        # Reject entity declarations before parsing, including UTF-16/32 encodings.
        if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", raw.replace(b"\x00", b""), re.I):
            raise IngestError("CORRUPT", "XML entity declarations are not supported")
        return ET.fromstring(raw)

    def relationships(self, part: str) -> dict[str, str]:
        directory, filename = posixpath.split(part)
        name = f"{directory}/_rels/{filename}.rels"
        if name not in self.zip.namelist():
            return {}
        relations = {}
        for relation in self.xml(name):
            if relation.get("TargetMode") == "External":
                continue
            target = relation.get("Target", "")
            resolved = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else posixpath.join(directory, target)
            )
            if resolved.startswith("../") or ":" in resolved or "\\" in resolved:
                raise IngestError("CORRUPT", "Invalid Office relationship")
            relations[relation.get("Id", "")] = resolved
        return relations


def paragraph(node: ET.Element, prefix: str) -> str:
    pieces = []
    for item in node.iter():
        if item.tag == tag(prefix + ":t"):
            pieces.append(item.text or "")
        elif item.tag == tag(prefix + ":tab"):
            pieces.append("\t")
        elif item.tag in (tag(prefix + ":br"), tag(prefix + ":cr")):
            pieces.append("\n")
    return "".join(pieces)


def read(path: Path, mime: str, limit: int = 500) -> Structure:
    try:
        if mime == MARKDOWN:
            text = path.read_text(encoding="utf-8-sig")
            if "\x00" in text:
                raise IngestError("CORRUPT", "Markdown must be UTF-8 text without NUL")
            return read_markdown(text)
        package = Package(path)
        try:
            return read_slides(package, limit) if mime == PPTX else read_word(package)
        finally:
            package.zip.close()
    except IngestError:
        raise
    except (
        BadZipFile,
        KeyError,
        ET.ParseError,
        UnicodeError,
        ValueError,
        RuntimeError,
        NotImplementedError,
    ) as error:
        raise IngestError("CORRUPT", "Could not read the document's native structure") from error


def read_markdown(text: str) -> Structure:
    result = Structure()
    lines = text.replace("\r\n", "\n").splitlines()
    n = 0
    while n < len(lines):
        line = lines[n]
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            block = [line]
            marker = fence[1]
            n += 1
            while n < len(lines):
                block.append(lines[n])
                n += 1
                if re.fullmatch(
                    r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*", block[-1]
                ):
                    break
            result.body("\n".join(block))
            continue
        heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$", line)
        if heading:
            result.heading(re.sub(r"\s+#+\s*$", "", heading[2]), len(heading[1]) - 1)
        elif line.strip() and n + 1 < len(lines) and re.fullmatch(r" {0,3}(=+|-+)\s*", lines[n + 1]):
            result.heading(line.strip(), 0 if lines[n + 1].lstrip().startswith("=") else 1)
            n += 1
        elif (
            "|" in line
            and n + 1 < len(lines)
            and all(re.fullmatch(r":?-{3,}:?", c) for c in cells(lines[n + 1]))
        ):
            rows = [cells(line)]
            n += 2
            while n < len(lines) and "|" in lines[n] and lines[n].strip():
                rows.append(cells(lines[n]))
                n += 1
            result.table(rows, header=True)
            continue
        else:
            # Preserve lists, indented code and source syntax; never render embedded HTML.
            block = [line]
            while (
                n + 1 < len(lines)
                and lines[n + 1].strip()
                and (lines[n + 1].startswith("    ") or line.startswith("    "))
            ):
                n += 1
                block.append(lines[n])
            result.body("\n".join(block))
        n += 1
    return result


def cells(line: str) -> tuple[str, ...]:
    return tuple(c.strip().replace(r"\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|")))


def read_slides(package: Package, limit: int) -> Structure:
    result = Structure()
    part = "ppt/presentation.xml"
    presentation = package.xml(part)
    relations = package.relationships(part)
    slides = presentation.findall("p:sldIdLst/p:sldId", NS)
    if len(slides) > limit:
        raise IngestError("TOO_LARGE", "Too many slides")
    seen_parts: set[str] = set()
    for number, ref in enumerate(slides, 1):
        slide_part = relations[ref.attrib[tag("r:id")]]
        if slide_part in seen_parts:
            raise IngestError("CORRUPT", "Duplicate slide relationship")
        seen_parts.add(slide_part)
        slide = package.xml(slide_part)
        shapes = list(slide.iter(tag("p:sp")))
        titles: list[str] = []
        for shape in shapes:
            placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
            if placeholder is not None and placeholder.get("type") in ("title", "ctrTitle"):
                titles.extend(paragraph(p, "a") for p in shape.findall("p:txBody/a:p", NS))
        title = " ".join(t for t in titles if t.strip()) or f"Slide {number}"
        result.heading(title, 0, slide=number)
        _slide_content(slide, result)
        for related in package.relationships(slide_part).values():
            if related.startswith("ppt/notesSlides/") and related.endswith(".xml"):
                notes = package.xml(related)
                for shape in notes.iter(tag("p:sp")):
                    placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
                    if placeholder is not None and placeholder.get("type") == "body":
                        text = "\n".join(paragraph(p, "a") for p in shape.findall("p:txBody/a:p", NS))
                        result.body("Speaker notes:\n" + text if text.strip() else "")
    result.unit = len(slides)
    return result


def _slide_content(slide: ET.Element, result: Structure) -> None:
    tree = slide.find("p:cSld/p:spTree", NS)
    if tree is not None:
        for shape in tree.iter():
            if shape.tag == tag("p:sp"):
                placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
                if placeholder is not None and placeholder.get("type") in (
                    "title",
                    "ctrTitle",
                    "dt",
                    "ftr",
                    "sldNum",
                ):
                    continue
                for p in shape.findall("p:txBody/a:p", NS):
                    props = p.find("a:pPr", NS)
                    level = int(props.get("lvl", "0")) if props is not None else 0
                    result.body("  " * min(level, 9) + paragraph(p, "a"))
            elif shape.tag == tag("a:tbl"):
                rows = [
                    tuple(
                        "\n".join(paragraph(p, "a") for p in cell.findall("a:txBody/a:p", NS))
                        for cell in row.findall("a:tc", NS)
                    )
                    for row in shape.findall("a:tr", NS)
                ]
                props = shape.find("a:tblPr", NS)
                result.table(rows, header=props is not None and props.get("firstRow") in ("1", "true"))


def read_word(package: Package) -> Structure:
    result = Structure()
    styles = {}
    if "word/styles.xml" in package.zip.namelist():
        styles = {
            s.get(tag("w:styleId"), ""): s for s in package.xml("word/styles.xml").findall("w:style", NS)
        }

    document = package.xml("word/document.xml")
    body = document.find("w:body", NS)
    if body is None:
        raise IngestError("CORRUPT", "Word document body is missing")
    for item in body:
        if item.tag == tag("w:p"):
            text = paragraph(item, "w")
            if not text.strip():
                continue
            props = item.find("w:pPr", NS)
            heading_level = word_level(props, styles)
            if heading_level is not None:
                result.heading(text, heading_level)
            else:
                if props is not None and props.find("w:numPr", NS) is not None:
                    text = "• " + text
                result.body(text)
        elif item.tag == tag("w:tbl"):
            rows = [
                tuple(
                    "\n".join(paragraph(p, "w") for p in cell.findall("w:p", NS))
                    for cell in row.findall("w:tc", NS)
                )
                for row in item.findall("w:tr", NS)
            ]
            header = item.find("w:tr/w:trPr/w:tblHeader", NS)
            result.table(
                rows,
                header=header is not None and header.get(tag("w:val"), "true") not in ("0", "false", "off"),
            )
    return result


def word_level(
    props: ET.Element | None, styles: dict[str, ET.Element], visited: frozenset[str] = frozenset()
) -> int | None:
    if props is None:
        return None
    outline = props.find("w:outlineLvl", NS)
    if outline is not None:
        value = int(outline.get(tag("w:val"), "9"))
        return value if 0 <= value < 9 else None
    style_ref = props.find("w:pStyle", NS)
    style_id = style_ref.get(tag("w:val"), "") if style_ref is not None else ""
    if style_id in visited or style_id not in styles:
        return None
    style = styles[style_id]
    styled_props = style.find("w:pPr", NS)
    styled = word_level(styled_props, styles, visited | {style_id})
    if styled is not None or (styled_props is not None and styled_props.find("w:outlineLvl", NS) is not None):
        return styled
    parent = style.find("w:basedOn", NS)
    if parent is not None:
        inherited = ET.Element(tag("w:pPr"))
        ET.SubElement(inherited, tag("w:pStyle"), {tag("w:val"): parent.get(tag("w:val"), "")})
        return word_level(inherited, styles, visited | {style_id})
    return None
