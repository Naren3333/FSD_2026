"""offline probe fixtures."""

import json
from pathlib import Path

import pymupdf
import pytest

from pdf_structure_probe import extract, write_review


def sample(path: Path) -> Path:
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 100), "Parent heading", fontsize=12)
        page.insert_text((50, 140), "Child heading", fontsize=12)
        page.insert_text((50, 180), "A paragraph with enough native text to inspect and retain.", fontsize=12)
        page.insert_text((50, 220), "1.2.3 Same style heading", fontsize=12)
        page.insert_text((50, 260), "1. An ordered list item", fontsize=12)
        doc.set_toc([[1, "Parent heading", 1], [2, "Child heading", 1]])
        doc.new_page().set_rotation(90)
        doc.save(path)
    return path


def test_bookmarks_geometry_numbering_and_all_lines_survive(tmp_path: Path) -> None:
    result = extract(sample(tmp_path / "source.pdf"))
    nodes = {e.id: e for e in result.elements}
    headings = {e.text: e for e in result.elements if e.type == "heading"}
    child = headings["Child heading"]
    section = nodes[child.parent_id]
    assert nodes[section.parent_id].attributes["heading_id"] == headings["Parent heading"].id
    assert headings["1.2.3 Same style heading"].attributes["heading_level"] == 3
    item = next(e for e in result.elements if e.type == "list_item_candidate")
    assert item.attributes["marker"] == "1."
    assert item.provenance[0]["geometry"] == "measured"
    assert item.provenance[0]["page"] == 1
    assert item.provenance[0]["bbox_pt"][0] == pytest.approx(50)
    assert len([e for e in result.elements if e.text]) == 5
    assert result.pages[1]["rotation_degrees"] == 90
    assert result.pages[1]["native_characters"] == 0
    assert any(w["code"] == "LOW_NATIVE_TEXT_OCR_NOT_ATTEMPTED" and w["page"] == 2 for w in result.warnings)


def test_no_text_is_not_manufactured_and_output_is_reproducible(tmp_path: Path) -> None:
    path = tmp_path / "blank.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    first = extract(path)
    assert first.payload() == extract(path).payload()
    assert not any(e.text for e in first.elements)
    assert first.payload()["status"] == "experimental-needs-review"
    assert first.warnings[0]["code"] == "LOW_NATIVE_TEXT_OCR_NOT_ATTEMPTED"


def test_review_never_overwrites_annotations(tmp_path: Path) -> None:
    result = extract(sample(tmp_path / "source.pdf"))
    folder = write_review(result, tmp_path / "reviews")
    annotations = folder / "annotations.json"
    assert json.loads(annotations.read_text())["status"] == "unannotated"
    annotations.write_text('{"status":"human-reviewed"}')
    with pytest.raises(FileExistsError):
        write_review(result, tmp_path / "reviews")
    assert json.loads(annotations.read_text())["status"] == "human-reviewed"
    assert (
        json.loads((folder / "structure.json").read_text(encoding="utf-8"))["root_id"]
        == result.elements[0].id
    )


def test_invalid_and_encrypted_inputs_fail_without_artifacts(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.pdf"
    invalid.write_bytes(b"not PDF")
    with pytest.raises(ValueError, match="PDF header"):
        extract(invalid)
    encrypted = tmp_path / "encrypted.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader")
    with pytest.raises(ValueError, match="Encrypted"):
        extract(encrypted)


def test_table_cells_are_alternate_views_with_native_provenance(tmp_path: Path) -> None:
    path = tmp_path / "grid.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        for x in [50, 200, 350]:
            page.draw_line((x, 100), (x, 180))
        for y in [100, 140, 180]:
            page.draw_line((50, y), (350, y))
        for x, y, text in [(60, 125, "Model"), (210, 125, "Owner"), (60, 165, "IaaS"), (210, 165, "User")]:
            page.insert_text((x, y), text)
        doc.save(path)
    result = extract(path)
    table = next(e for e in result.elements if e.type == "table_candidate")
    assert table.attributes["representation_role"] == "alternate-view-of-native-text"
    assert [c["text"] for c in table.attributes["cells"]] == ["Model", "Owner", "IaaS", "User"]
    assert all(c["provenance"][0]["page"] == 1 for c in table.attributes["cells"])
    assert any("IaaS" in e.text for e in result.elements)


def test_repeated_margin_text_is_retained_as_review_candidate(tmp_path: Path) -> None:
    path = tmp_path / "furniture.pdf"
    with pymupdf.open() as doc:
        for number in range(3):
            page = doc.new_page()
            page.insert_text((50, 30), f"Report page {number + 1}")
            page.insert_text(
                (50, 200), "Body content with enough native text to preserve through extraction."
            )
        doc.save(path)
    result = extract(path)
    furniture = [e for e in result.elements if e.type == "furniture_candidate"]
    assert len(furniture) == 3
    assert {e.text for e in furniture} == {"Report page 1", "Report page 2", "Report page 3"}
    assert all(e.attributes["excluded_from_retrieval_candidate"] for e in furniture)
