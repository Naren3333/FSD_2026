# ADR 0013: Native document formats next to PDF

Status: accepted 2026-10-21. The project owner chose PowerPoint, Markdown and plain text, keeping PDF.

## Context

A PDF records where text is drawn, not what it is: there is no heading, slide or table marker to read back. ai-core
rebuilds that structure from font sizes, the outline, column gutters and a selective vision pass (ADR 0012), and it
works for born-digital PDFs (the 22-PDF evaluation corpus: hit@1 0.924, MRR 0.950 on 2026-10-20). OCR is needed only
for scans. What remains hard is exactly what the layout hides: whether a region is a figure or a table (the two
`kind` failures in `docs/development/rag-quality-followup.md`), and every vision call costs the uploading teacher.

Lecturers usually have the source file. A `.pptx` says which text is a slide's title, which shapes are tables and
charts, and where each slide begins; Markdown says where its headings and tables are. Text files were accepted
before this decision but only cut into 1,000-character pieces and never indexed, so the assistant and question
generation could not use them.

## Decision

- Content accepts PDF and PowerPoint `.pptx` (50 MiB), Markdown and UTF-8 plain text (5 MiB). Every upload is
  `indexing` and goes to ai-core's `/v1/ingest` as its own media type; nothing is chunked on upload. Older `.ppt`
  files and password-protected decks are refused (they are OLE files, not zips).
- ai-core reads each format's own structure into the same paragraphs, headings, tables and figures the PDF parser
  produces, so chunking, embedding, search, reranking and citations are unchanged:
  - a deck slide by slide (a slide is a page; hidden slides keep their numbers): title placeholders as headings,
    PowerPoint sections above them, text boxes, tables, chart data as tables, SmartArt text and speaker notes; only
    pictures covering 5% of a slide are described by the figure model, and one repeated on three or more slides is
    decoration;
  - Markdown by its ATX and setext headings, pipe tables and fenced code; plain text as paragraphs only. Neither has
    pages: they report one page and are located by section path.
- Text chunks keep `source` `pdf`, meaning the document's own text whatever its format; `vision` still marks text a
  model wrote.
- Search results and chat citations carry the document's `mime_type`, so the web app says "slide 7" for a deck,
  gives no page for a text file, and opens only a PDF at a page. Citations saved earlier default to PDF.
- Content migration 006 queues every text file uploaded before this decision for indexing.

## Consequences

A deck costs one model call per described picture instead of a vision pass over its pages, and text costs only its
embeddings; reindexing either is free (descriptions and embeddings are cached). The PDF pipeline is unchanged and
stays the fallback for documents that only exist as PDFs, including scans. python-pptx (with lxml and Pillow) joins
ai-core's dependencies. Retrieval quality on decks is not yet measured against the PDF baseline: the evaluation
corpus is PDFs, and comparing a deck with its exported PDF is the next measurement.
