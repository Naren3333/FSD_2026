# ADR 0013: Native structured documents

Accepted: 2026-10-21. The owner authorized native heading-aware handling of Markdown, PPTX and DOCX, and deferred PDF improvements and test execution.

## Decision

Keep the ten applications and existing ownership. Content accepts `text/markdown` (5 MiB), PPTX and DOCX OOXML MIME types (50 MiB), stores the original and queues the existing durable indexing worker. The worker forwards the recorded MIME type to ai-core. Plain `.txt` keeps its existing extraction-only behavior. Existing PDF extraction, selective vision and chunk settings remain unchanged.

ai-core reads native structure locally, without Office execution, converters or model extraction calls:

- Markdown: ATX/Setext headings, fenced/indented code text, lists and pipe tables. The reader preserves source syntax; it is not a complete CommonMark renderer.
- PPTX: presentation relationship order, slide titles, body text, native tables and speaker-note text. Notes belong to their slide and become teaching material visible to authorized classroom members; the upload form explicitly says notes are included. Slide titles introduce sections.
- DOCX: paragraph outline levels, heading-style inheritance, body/list paragraphs and native tables. Repeated page headers/footers are not treated as section headings. Actual pagination is not inferred from Word XML.

The readers emit the existing paragraph/table structure and use the same token-bounded chunker. Each native section (or slide) is a boundary, so chunks do not cross sibling sections. Heading ancestry is stored in `section_path` and included in full in native embedding and reranking inputs; PDF inputs retain their existing deepest-heading rule. Consecutive headings with no body remain together to avoid unnecessary title-only chunks. Native preparation and exact embedding inputs are cached; only uncached embedding work calls a model, through existing ai-core billing/reservations and allowance checks. Literal tokenizer marker strings in source/code blocks are treated as ordinary text by the shared chunker.

## Location compatibility

Extend chunk `source` with `markdown`, `pptx`, and `docx` using a data-preserving constraint migration. Existing `pdf`/`vision` chunks and originals are retained; nothing is automatically reindexed.

For compatibility, `page_start`, `page_end` and `page_count` remain numeric fields. For PPTX they identify slides. For Markdown/DOCX they are internal 1-based logical section anchors/counts, **not rendered pages**. Chat prompts and the browser must use the source type: PDF pages, PPTX slides, or Markdown/Word section paths. Citations now also carry optional `section_path`; older saved citations remain valid. Escaped native section attributes are counted in both token and character context budgets. Original-file opening only adds PDF page fragments for PDF sources. Question generation receives native format/section context and does not report native anchors as PDF pages in copied evidence.

## Bounds and limitations

Reject Office archives over 4096 entries or 64 MiB expanded size, XML parts over 8 MiB, extracted text over 8 MiB, duplicate archive entries, entity declarations, invalid relationship traversal, and documents exceeding existing source-unit/chunk limits. Native chunk content is capped at the rerank contract's 20,000 characters, heading paths at 20,000 characters, and each fully assembled native embedding input at 2,200 tokens so existing 128-input batches stay bounded. Oversized structure fails before embeddings instead of silently truncating headings. Cached documents are checked against current source-unit/chunk limits too. Relationships are read from the archive only; external resources are never fetched. No macros or Office applications execute.

Embedded images, charts, SmartArt, equations and complex merged-table layout are not interpreted by this initial native reader. Word text boxes/content controls, arbitrary numbering formats and complete Markdown dialect support are not promised. For visual material the teacher can choose an existing PDF export workflow; further PDF work remains deferred.

## Validation status

Parser/API, transport, source-label and format-selection fixtures accompany the change. The owner will run tests later: do not treat added tests as passing evidence. Formatting, lint, type/build and contract-generation results are recorded in the handover. No paid AI evaluation or test-stack deployment is part of this implementation turn.
