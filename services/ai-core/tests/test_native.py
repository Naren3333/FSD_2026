"""Native structure, provenance and bounded parsing; fixtures contain actual OOXML parts."""

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from conftest import FakeOpenAI, ingested
from fastapi.testclient import TestClient

from ingest import native
from ingest.chunker import chunk_paras
from ingest.parse import IngestError


def office(parts: dict[str, str]) -> bytes:
    data = BytesIO()
    with ZipFile(data, "w", ZIP_DEFLATED) as package:
        for name, content in parts.items():
            package.writestr(name, content)
    return data.getvalue()


def slides() -> bytes:
    namespaces = f'xmlns:p="{native.NS["p"]}" xmlns:a="{native.NS["a"]}"'

    def slide(title: str, body: str) -> str:
        return f"""<p:sld {namespaces}><p:cSld><p:spTree>
        <p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
        <p:txBody><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>
        <p:sp><p:txBody><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody></p:sp>
        </p:spTree></p:cSld></p:sld>"""

    return office(
        {
            "ppt/presentation.xml": f'''<p:presentation xmlns:p="{native.NS["p"]}" xmlns:r="{native.NS["r"]}">
        <p:sldIdLst><p:sldId r:id="second"/><p:sldId r:id="first"/></p:sldIdLst></p:presentation>''',
            "ppt/_rels/presentation.xml.rels": """<Relationships>
        <Relationship Id="first" Target="slides/slide1.xml"/>
        <Relationship Id="second" Target="slides/slide2.xml"/></Relationships>""",
            "ppt/slides/slide1.xml": slide(
                "Transactions", "A transaction groups related changes atomically."
            ),
            "ppt/slides/slide2.xml": slide(
                "Storage", "A repository separates storage access from application logic."
            ),
            "ppt/slides/_rels/slide2.xml.rels": """<Relationships>
        <Relationship Id="notes" Target="../notesSlides/notesSlide2.xml"/>
        <Relationship Id="external" Target="https://example.invalid/private" TargetMode="External"/>
        </Relationships>""",
            "ppt/notesSlides/notesSlide2.xml": f"""<p:notes {namespaces}><p:sp><p:nvSpPr><p:nvPr>
        <p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r>
        <a:t>Explain the repository interface before showing implementation.</a:t>
        </a:r></a:p></p:txBody></p:sp></p:notes>""",
        }
    )


def word() -> bytes:
    ns = f'xmlns:w="{native.NS["w"]}"'
    return office(
        {
            "word/styles.xml": f"""<w:styles {ns}>
        <w:style w:styleId="Heading1"><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>
        <w:style w:styleId="Custom"><w:basedOn w:val="Heading1"/></w:style>
        <w:style w:styleId="Heading2"><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>
        </w:styles>""",
            "word/document.xml": f"""<w:document {ns}><w:body>
        <w:p><w:pPr><w:pStyle w:val="Custom"/></w:pPr><w:r><w:t>Storage</w:t></w:r></w:p>
        <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>PostgreSQL</w:t></w:r></w:p>
        <w:p><w:r><w:t>Transactions group changes and preserve database consistency.</w:t></w:r></w:p>
        <w:tbl><w:tr><w:trPr><w:tblHeader/></w:trPr>
        <w:tc><w:p><w:r><w:t>Property</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Meaning</w:t></w:r></w:p></w:tc></w:tr>
        <w:tr><w:tc><w:p><w:r><w:t>Atomicity</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>All or nothing</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Redis</w:t></w:r></w:p>
        <w:p><w:r><w:t>Redis stores keyed values for fast lookup.</w:t></w:r></w:p>
        </w:body></w:document>""",
        }
    )


def test_markdown_hierarchy_fences_tables_and_section_boundaries():
    doc = native.read_markdown("""# Storage
## PostgreSQL
Transactions preserve consistency across related changes.
```python
# not a document heading
commit()
```
| Property | Meaning |
| --- | --- |
| Atomicity | All or nothing |
### Isolation
Concurrent work should not interfere with another transaction.
## Redis
Keyed values support a different access pattern.
""")
    chunks = chunk_paras(doc.paras, page_boundaries=True)
    assert any(c.section_path == "Storage > PostgreSQL > Isolation" for c in chunks)
    assert all("not a document heading" not in (c.section_path or "") for c in chunks)
    assert any(c.kind == "table" and "Property: Atomicity" in (c.embed_body or "") for c in chunks)
    assert not any("Redis" in c.content and "commit()" in c.content for c in chunks)


def test_word_styles_are_inherited_and_table_headers_preserved(tmp_path: Path):
    path = tmp_path / "material.docx"
    path.write_bytes(word())
    doc = native.read(path, native.DOCX)
    chunks = chunk_paras(doc.paras, page_boundaries=True)
    assert any(c.section_path == "Storage > PostgreSQL" for c in chunks)
    assert any(c.section_path == "Storage > Redis" for c in chunks)
    assert any(c.kind == "table" and "Meaning: All or nothing" in (c.embed_body or "") for c in chunks)


def test_pptx_uses_presentation_order_and_attaches_notes_to_the_right_slide(tmp_path: Path):
    path = tmp_path / "material.pptx"
    path.write_bytes(slides())
    doc = native.read(path, native.PPTX)
    assert doc.unit == 2
    assert doc.paras[0].text == "Storage" and doc.paras[0].page == 1
    note = next(p for p in doc.paras if "Speaker notes" in p.text)
    assert note.page == 1 and note.path == ("Storage",)
    assert next(p for p in doc.paras if p.text == "Transactions").page == 2


@pytest.mark.parametrize(
    "mime,data",
    [
        (
            native.MARKDOWN,
            b"# Storage\n\nRepositories isolate data access from other application responsibilities.",
        ),
        (native.PPTX, slides()),
        (native.DOCX, word()),
    ],
)
def test_native_endpoint_embeds_structure_without_vision(
    api: TestClient, embeddings: FakeOpenAI, mime: str, data: bytes
):
    first = ingested(api, data, content_type=mime)
    assert first["outcome"] == "indexed"
    assert first["ocr_page_count"] == 0 and first["figures"] == []
    assert all(c["source"] == native.FORMATS[mime] and c["section_path"] for c in first["chunks"])
    assert all(len(c["embedding"]) == 1536 for c in first["chunks"])
    assert all(c["section_path"] in c["embed_text"] for c in first["chunks"])
    count = len(embeddings.requests)
    assert ingested(api, data, content_type=mime) == first
    assert len(embeddings.requests) == count  # exact embedding/preparation reuse
    assert all("input" in request for request in embeddings.requests)  # no image/model inference requests


def test_office_limits_and_entity_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "material.docx"
    path.write_bytes(office({"word/document.xml": '<!DOCTYPE x [<!ENTITY secret "unsafe">]><x/>'}))
    with pytest.raises(IngestError, match="entity"):
        native.read(path, native.DOCX)
    path.write_bytes(word())
    monkeypatch.setattr(native, "MAX_EXPANDED", 10)
    with pytest.raises(IngestError) as failure:
        native.read(path, native.DOCX)
    assert failure.value.reason == "TOO_LARGE"


def test_corrupt_markdown_does_not_reach_embeddings(api: TestClient, embeddings: FakeOpenAI):
    assert ingested(api, b"\xff", content_type=native.MARKDOWN)["failure_reason"] == "CORRUPT"
    assert embeddings.requests == []


def test_native_code_treats_tokenizer_markers_as_literal_text():
    doc = native.read_markdown("# Code\n\n```text\n<|endoftext|> is a string in this example.\n```")
    chunks = chunk_paras(doc.paras, page_boundaries=True)
    assert "<|endoftext|>" in chunks[0].content


def test_markdown_setext_and_indented_code_do_not_create_false_headings():
    doc = native.read_markdown(
        "Storage\n=======\n\n    # this is code\n\n## C#\nA language example with a literal hash suffix."
    )
    assert [p.text for p in doc.paras if p.level is not None] == ["Storage", "C#"]
