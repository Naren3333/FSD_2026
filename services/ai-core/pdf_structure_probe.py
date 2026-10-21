"""offline, native-text pdf structure experiment; never imported by the running app."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

VERSION = "native-structure-probe.v1"
MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 500
MAX_ELEMENTS = 50000
MAX_CHARS = 8 * 1024 * 1024
NUMBERED = re.compile(r"^(\d+(?:\.\d+)+)\.?\s+\S")
LIST_ITEM = re.compile(r"^([•▪◦‣●○■□►▸–-])\s*(\S.*)$|^(\d+[.)]|[a-zA-Z][.)])\s+(\S.*)$")


@dataclass
class Element:
    id: str
    type: str
    parent_id: str | None
    text: str = ""
    children: list[str] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Structure:
    source_sha256: str
    source_name: str
    metadata: dict[str, Any]
    pages: list[dict[str, Any]] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    _characters: int = 0

    def add(self, kind: str, parent: Element | None, text: str = "", **attrs: Any) -> Element:
        self._characters += len(text)
        if len(self.elements) >= MAX_ELEMENTS or self._characters > MAX_CHARS:
            raise ValueError("Extracted structure exceeds the experiment's element/text limit")
        item = Element(f"e{len(self.elements):06d}", kind, parent.id if parent else None, text)
        item.attributes = attrs
        self.elements.append(item)
        if parent:
            parent.children.append(item.id)
        return item

    def warn(self, code: str, page: int | None = None, **details: Any) -> None:
        self.warnings.append({"code": code, "page": page, **details})

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "pdf-structure-experiment.v1",
            "extractor": VERSION,
            "pymupdf_version": pymupdf.VersionBind,
            "source_sha256": self.source_sha256,
            "source_name": self.source_name,
            "extraction_id": hashlib.sha256(
                f"{self.source_sha256}|{VERSION}|{pymupdf.VersionBind}".encode()
            ).hexdigest(),
            "status": "experimental-needs-review",
            "metadata": self.metadata,
            "root_id": self.elements[0].id,
            "pages": self.pages,
            "elements": [asdict(e) for e in self.elements],
            "warnings": self.warnings,
            "limitations": [
                "Native text only: missing/broken text needs later OCR; no OCR was attempted.",
                "Line elements retain source blocks; paragraph/list grouping needs annotation.",
                "Reading order is PyMuPDF sort order, not validated multi-column order.",
                "Headings, nesting, furniture and tables are candidates, not ground truth.",
                "Images are detected regions; vector-only figures, captions and semantics are unresolved.",
                "Table views reference retained text; do not embed both views as separate evidence.",
                "Coordinates are unrotated PyMuPDF page coordinates, not screen pixels.",
            ],
        }


def provenance(page: int, box: Any) -> list[dict[str, Any]]:
    return [
        {
            "page": page,
            "bbox_pt": list(box),
            "coordinate_space": "pymupdf-unrotated-page",
            "geometry": "measured",
            "text_method": "native",
        }
    ]


def _lines(page: pymupdf.Page) -> list[dict[str, Any]]:
    result = []
    for block_no, block in enumerate(
        page.get_text("dict", sort=True, flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)[
            "blocks"
        ]
    ):
        if block["type"] != 0:
            continue
        for line_no, line in enumerate(block["lines"]):
            spans = [s for s in line["spans"] if s["text"]]
            text = "".join(s["text"] for s in spans)
            if not text.strip():
                continue
            size = max((s["size"] for s in spans), default=0)
            result.append(
                {
                    "text": text,
                    "bbox": line["bbox"],
                    "block": block_no,
                    "line": line_no,
                    "size": size,
                    "bold": sum(len(s["text"]) for s in spans if s["flags"] & 16) > len(text) / 2,
                    "spans": [
                        {
                            "text": s["text"],
                            "bbox_pt": list(s["bbox"]),
                            "size_pt": s["size"],
                            "font": s["font"],
                            "flags": s["flags"],
                            "alpha": s.get("alpha"),
                        }
                        for s in spans
                    ],
                }
            )
    return result


def _body_size(pages: list[list[dict[str, Any]]]) -> float:
    counts: Counter[float] = Counter()
    for lines in pages:
        for line in lines:
            counts[round(line["size"] * 2) / 2] += len(line["text"])
    return counts.most_common(1)[0][0] if counts else 12.0


def _furniture_key(line: dict[str, Any], height: float) -> tuple[str, int] | None:
    x0, top, x1, bottom = line["bbox"]
    del x0, x1
    if len(line["text"]) > 120 or (top > height * 0.12 and bottom < height * 0.88):
        return None
    band = 0 if top < height * 0.12 else 1
    return re.sub(r"\d+", "#", " ".join(line["text"].casefold().split())), band


def _heading(
    line: dict[str, Any], body: float, bookmarks: list[tuple[int, str]], sizes: list[float]
) -> tuple[int | None, str]:
    text = " ".join(line["text"].split())
    for level, title in bookmarks:
        if text.casefold() == " ".join(title.casefold().split()):
            return level, "exact-page-bookmark"
    if len(text) > 150 or text.endswith((".", ",", ";")) or LIST_ITEM.match(text):
        return None, "body-or-list"
    numbered = NUMBERED.match(text)
    if numbered:
        return numbered[1].count(".") + 1, "numbering-candidate"
    if line["size"] >= body * 1.15:
        return sizes.index(round(line["size"] * 2) / 2) + 1, "relative-size-candidate"
    if line["bold"] and line["size"] >= body * 0.95:
        return len(sizes) + 1, "body-size-bold-candidate"
    return None, "unclassified-native-line"


def _text_tree(structure: Structure, root: Element, raw: list[list[dict[str, Any]]], toc: list[Any]) -> None:
    body = _body_size(raw)
    sizes = sorted(
        {round(line["size"] * 2) / 2 for lines in raw for line in lines if line["size"] >= body * 1.15},
        reverse=True,
    )
    repeats: Counter[tuple[str, int]] = Counter()
    for page, lines in zip(structure.pages, raw, strict=True):
        repeats.update({key for line in lines if (key := _furniture_key(line, page["height_pt"]))})
    repeated = {key for key, count in repeats.items() if len(raw) >= 3 and count / len(raw) > 0.6}
    stack: list[tuple[int, Element, str]] = []
    for page, lines in zip(structure.pages, raw, strict=True):
        number = page["number"]
        if page["width_pt"] > page["height_pt"]:
            stack = []  # explicit slide boundary; no invented section heading.
        bookmarks = [(int(t[0]), str(t[1])) for t in toc if t[2] == number]
        for order, line in enumerate(lines):
            parent = stack[-1][1] if stack else root
            role = _furniture_key(line, page["height_pt"]) in repeated
            level, method = (
                (None, "repeated-margin-candidate") if role else _heading(line, body, bookmarks, sizes)
            )
            path = [entry[2] for entry in stack]
            if level is not None:
                while stack and stack[-1][0] >= level:
                    stack.pop()
                if level > (stack[-1][0] + 1 if stack else 1):
                    structure.warn("HEADING_DEPTH_JUMP", number, text=line["text"], level=level)
                parent = structure.add("section", stack[-1][1] if stack else root, heading_level=level)
                path = [entry[2] for entry in stack] + [line["text"]]
                stack.append((level, parent, line["text"]))
            kind = "furniture_candidate" if role else "heading" if level is not None else "text_line"
            marker = LIST_ITEM.match(line["text"])
            if marker and kind == "text_line":
                kind = "list_item_candidate"
            item = structure.add(
                kind,
                root if role else parent,
                line["text"],
                heading_level=level,
                heading_path=path,
                classification_method=method,
                reading_order=order,
                source_block=line["block"],
                source_line=line["line"],
                spans=line["spans"],
                marker=(marker[1] or marker[3]) if marker else None,
                excluded_from_retrieval_candidate=role,
            )
            item.provenance = provenance(number, line["bbox"])
            if level is not None:
                parent.attributes["heading_id"] = item.id


def _region_parent(structure: Structure, page: int, box: Any) -> Element:
    # conservative location match; unresolved/spanning regions stay at the root.
    candidates = [
        e
        for e in structure.elements
        if e.type == "heading" and e.provenance[0]["page"] == page and e.provenance[0]["bbox_pt"][3] <= box[1]
    ]
    parent = candidates[-1].parent_id if candidates else structure.elements[0].id
    return next(e for e in structure.elements if e.id == parent)


def _regions(structure: Structure, doc: pymupdf.Document) -> None:
    cell_count = 0
    for page in doc.pages():
        number = page.number + 1
        for image in page.get_image_info():
            box = image["bbox"]
            item = structure.add(
                "image_region",
                _region_parent(structure, number, box),
                interpretation="not-attempted",
                width_pixels=image["width"],
                height_pixels=image["height"],
            )
            item.provenance = provenance(number, box)
        try:
            tables = page.find_tables(strategy="lines").tables
            for table in tables:
                cell_count += table.row_count * table.col_count
                if cell_count > MAX_ELEMENTS:
                    raise ValueError("Table cells exceed experiment limits")
                item = structure.add(
                    "table_candidate",
                    _region_parent(structure, number, table.bbox),
                    rows=table.row_count,
                    columns=table.col_count,
                    cells=[],
                    representation_role="alternate-view-of-native-text",
                )
                item.provenance = provenance(number, table.bbox)
                for row_no, (row, values) in enumerate(zip(table.rows, table.extract(), strict=True)):
                    for col_no, (box, value) in enumerate(zip(row.cells, values, strict=True)):
                        structure._characters += len(value or "")
                        if structure._characters > MAX_CHARS:
                            raise ValueError("Table text exceeds experiment limits")
                        item.attributes["cells"].append(
                            {
                                "row": row_no,
                                "column": col_no,
                                "text": value,
                                "row_span": None,
                                "column_span": None,
                                "provenance": provenance(number, box) if box is not None else [],
                            }
                        )
                structure.warn("TABLE_STRUCTURE_UNVERIFIED", number, element_id=item.id)
        except RuntimeError as exc:
            structure.warn("TABLE_DETECTION_FAILED", number, detail=str(exc))


def extract(path: Path) -> Structure:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("PDF exceeds 50 MiB")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("Input does not have a PDF header")
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("Encrypted PDF requires a password; no extraction attempted")
        if not 1 <= doc.page_count <= MAX_PAGES:
            raise ValueError("PDF must have 1–500 pages")
        structure = Structure(digest, path.name, dict(doc.metadata or {}))
        root = structure.add("document", None)
        raw: list[list[dict[str, Any]]] = []
        chars = 0
        for page in doc.pages():
            lines = _lines(page)
            chars += sum(len(line["text"]) for line in lines)
            if chars > MAX_CHARS or sum(len(p) for p in raw) + len(lines) > MAX_ELEMENTS:
                raise ValueError("Native text exceeds experiment limits")
            raw.append(lines)
            structure.pages.append(
                {
                    "number": page.number + 1,
                    "pdf_label": page.get_label(),
                    "width_pt": page.cropbox.width,
                    "height_pt": page.cropbox.height,
                    "cropbox_pt": list(page.cropbox),
                    "rotation_degrees": page.rotation,
                    "rotation_matrix": list(page.rotation_matrix),
                    "native_characters": sum(len(line["text"]) for line in lines),
                }
            )
            if sum(len(line["text"].strip()) for line in lines) < 50:
                structure.warn("LOW_NATIVE_TEXT_OCR_NOT_ATTEMPTED", page.number + 1)
            if any("\ufffd" in line["text"] for line in lines):
                structure.warn("REPLACEMENT_CHARACTERS", page.number + 1)
        _text_tree(structure, root, raw, doc.get_toc())
        _regions(structure, doc)
        structure.warn("READING_ORDER_AND_STRUCTURE_REQUIRE_ANNOTATION")
        return structure


def markdown(structure: Structure) -> str:
    lines = [
        f"# Structure review: {structure.source_name}",
        "",
        "> Experimental native extraction; not accepted RAG evidence.",
        "",
    ]
    for item in structure.elements:
        if not item.provenance:
            continue
        location = item.provenance[0]
        lines += [
            f"<!-- {item.id} | {item.type} | PDF page {location['page']} | bbox {location['bbox_pt']} -->"
        ]
        if item.type == "heading":
            lines += [f"{'#' * min(6, item.attributes['heading_level'] + 1)} {item.text}", ""]
        elif item.type == "furniture_candidate":
            lines += [f"> [Furniture candidate retained] {item.text}", ""]
        elif item.type in {"text_line", "list_item_candidate"}:
            lines += [item.text, ""]
        else:
            lines += [f"[{item.type}: see JSON for measured region/structure]", ""]
    lines += [
        "## Extraction warnings",
        "",
        *[f"- {w['code']} (page {w['page']})" for w in structure.warnings],
    ]
    return "\n".join(lines) + "\n"


def write_review(structure: Structure, output: Path) -> Path:
    target = output / f"{Path(structure.source_name).stem}-{structure.source_sha256[:12]}"
    target.mkdir(parents=True, exist_ok=False)  # never overwrite an earlier review or annotations.
    payload = structure.payload()
    with (target / "structure.json").open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
    (target / "review.md").write_text(markdown(structure), encoding="utf-8")
    annotation = {
        "status": "unannotated",
        "source_sha256": structure.source_sha256,
        "extraction_id": payload["extraction_id"],
        "reviewed_pages": [],
        "instructions": "Record independent ground truth; parser candidates are not gold labels.",
        "elements": [],
        "reading_order_edges": [],
        "notes": [],
    }
    (target / "annotations.json").write_text(json.dumps(annotation, indent=2) + "\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=Path(".local/pdf-structure"))
    args = parser.parse_args()
    failures = 0
    for path in args.pdf:
        try:
            structure = extract(path)
            target = write_review(structure, args.output)
            print(
                f"{path.name}: {len(structure.pages)} pages, {len(structure.elements)} elements, {len(structure.warnings)} warnings -> {target}"
            )
        except (OSError, ValueError, RuntimeError) as exc:
            failures += 1
            print(f"{path}: extraction/review failed: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
