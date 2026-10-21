# Structure-aware PDF extraction for Learning Ledger

Research date: 2026-10-21. Status: proposal for review; no pipeline implementation authorized by this report.

## 1. Recommendation and evidence boundaries

Retain the existing PyMuPDF pipeline as the baseline and native text/coordinate provider. Introduce a parser-independent structured document representation before changing retrieval. Evaluate Docling's standard PDF pipeline as the leading challenger for complex layouts and tables, with heading hierarchy explicitly enabled. Compare PyMuPDF4LLM as the lighter alternative. Adopt a routed hybrid only if the benchmark shows that routing preserves quality; if Docling consistently improves structural fidelity across the corpus, using it as the primary structural parser may be simpler than maintaining a fragile router.

The highest-value change is preserving structure and provenance through extraction, storage, chunking and retrieval. Replacing a parser while flattening its output immediately would lose much of the benefit. Do not build a custom computer vision model now. Use OCR only where usable text is missing or demonstrably corrupt, and reserve optional vision interpretation for relationships that text and layout cannot recover.

Evidence categories throughout this report:

- **Observed:** repository code/configuration and local PDF inspection described below.
- **Verified externally:** documented capabilities, defaults and licenses, with official links.
- **Proposed:** architecture, schemas, evaluation thresholds and roadmap. These are not implemented or measured results.
- **Unknown:** comparative accuracy, local end-to-end latency, peak memory, and compatibility of new dependencies with the current lockfile. No competing parser was installed or run.

Only this Markdown report was created. Application source, dependencies, infrastructure, migrations, caches and uploaded documents were not changed. No tests, reindexing, model downloads or paid AI calls were run. Existing PyMuPDF was used for read-only inspection and in-memory rendering. Pillow was unavailable; rendering used PyMuPDF directly instead of installing anything. The owner's aggregate $5 paid-call cap remains applicable to later work.

## 2. Existing project findings

All repository paths below are relative to `C:\Users\Naren\Documents\PROJECTSS\FSD_2026`; links use that absolute root.

### Purpose, architecture and ownership — observed

Learning Ledger supports classroom teaching materials, grounded questions and answers, assessments, teacher-approved grading and evidence-based learning analytics. There are exactly ten independently deployable business applications:

| Applications | Stack and responsibility |
|---|---|
| Identity/Classroom, Curriculum, Content, Assessment, Grading | Go, standard HTTP, explicit SQL/pgx; own their business data |
| Analytics | Python/FastAPI/Pydantic; deterministic derived metrics |
| ai-core | Python/FastAPI; extraction, embeddings, inference, reranking and billed usage |
| Question chatbot | Python/FastAPI; authorized retrieval, grounded answers, conversation persistence |
| Question generation | Python/FastAPI; teacher-facing generation and evidence validation |
| Student performance interpretation | Python/FastAPI; capability still returns 501 |

The browser is strict Angular 21. Local infrastructure uses Kong, Keycloak, Kafka, PostgreSQL with pgvector, and SeaweedFS through an S3-compatible interface. Neon is the intended managed PostgreSQL option; do not infer that the local stack is running on Neon. Content owns originals, document metadata, searchable chunks, vectors and figure crops. ai-core's PostgreSQL data is operational billing/reservations; it must not become the owner of educational document records.

Directory map: `apps/web` contains UI/API clients; `services` contains the ten apps and their migrations; `packages` contains operational/transport helpers; `contracts` contains generated API/event schemas; `infra` and Compose files define local infrastructure; `scripts` contains checks, integration and evaluation runners; `docs` contains requirements, architecture, ADRs and handover.

Read the historical requirements in conjunction with current ADRs: early instructions described an AI-free foundation, but subsequent ADRs authorize the implemented inference, ingest, retrieval, chatbot and teacher generation features. The research request does not authorize another implementation phase.

Primary references: [README](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/README.md), [requirements](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/requirements.md), [system architecture](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/architecture/system.md), [scalability](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/architecture/scalability.md), [current handover](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/development/handover.md), [AI integration ADR](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/decisions/0002-ai-integration.md), [spend ADR](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/decisions/0003-ai-spend.md), [efficiency ADR](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/decisions/0012-efficiency-and-durable-cleanup.md), [native formats ADR](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/decisions/0013-native-document-structure.md).

### Current ingestion and retrieval — observed

| Location | Existing behavior and significance |
|---|---|
| [Content handler](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/content/handler.go), [domain](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/content/domain.go), [store](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/content/store.go) | Authorized uploads; PDF magic validation, 50 MiB limit, original object and SHA-256 metadata. Native Markdown/PPTX/DOCX accepted in source; plain TXT remains extraction-only. |
| [Content indexing worker](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/content/index.go), [AI client](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/content/ai.go) | Leased jobs send original bytes and MIME to private ai-core `/v1/ingest`; bill the uploading teacher. Result validation, crops, chunk replacement and indexed event handling already exist. |
| [ai-core main](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/main.py), [ingest orchestration](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/__init__.py) | File validation, preparation cache, selective vision, parse/outline/chunk, exact-input embedding cache. Default limits: 500 pages, 5000 chunks; default chunks 600 tokens with 90-token overlap. |
| [Page primitives](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/pages.py), [parse](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/parse.py) | Native words/spans/boxes, image and drawing inspection, tables, repeated furniture removal, two-column ordering with spanning bands. Missing text can use vision transcription. |
| [Headings](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/headings.py) | Body-size estimation; short larger/bold blocks as candidates; repeated-style filtering; wrapped heading joining; matched multi-level bookmarks can supply hierarchy. Heuristics exclude bullets and some punctuation endings. |
| [Tables](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/tables.py), [selection](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/select.py), [vision](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/vision.py), [rescue](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/rescue.py) | Native ruled tables plus layout-assisted verification and borderless recovery. Selection retains uncertain graphics, broken text and tabular layouts. There are already figure/table/OCR model paths; this is not a text-only application. |
| [Chunker](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/chunker.py) | Heading paths, top-level boundaries and second-level bookmark boundaries; landscape-deck page boundaries; table row splitting with repeated headers; figure chunks, captions and label references. |
| [Content migration 002](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/content/migrations/002_indexing.sql) | `document_chunks`: document/ordinal, section path, page range, text, embedding text, token count, kind/source, image/labels/references; `vector(1536)`, HNSW and English full-text GIN. |
| [Content search](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/content/search.go), [chat](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/question-chatbot-agent/chat.py) | Authorized keyword/vector retrieval, reciprocal rank fusion, optional rerank/fallback, related figures/tables, bounded answer context and citations. No new vector service is needed. |
| [Native readers](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/native.py) | Markdown/PPTX/DOCX readers feed common chunk structures. Preserve their explicit section/slide semantics rather than routing native files through PDF. |

The requirements manifest and lockfile pin **PyMuPDF 1.28.2**. Docling, PyMuPDF4LLM, Unstructured, OpenCV and Tesseract are not declared in ai-core's runtime requirements. The container uses Python 3.13.7-slim. Any challenger needs a separate, approved compatibility/dependency trial before adoption.

The handover says the native-format source changes have static checks but deferred tests and deployment. This report does not establish which container image is currently serving requests.

### Structural gaps and failure risks — observed code, inferred consequences

1. **The durable result is chunks, not a document tree.** Local `Block` objects have geometry; `Para` has a heading path and optional table/figure; returned chunks lose body-element IDs, per-paragraph boxes, explicit list relationships and parent/child links. A flattened `section_path` cannot reliably reconstruct all of these later.
2. **PDF heading classification gates bookmark matching.** Candidates must first pass style rules. Same-size, same-weight headings can be missed despite meaningful numbering/position. Heading sizes normally form at most four style levels; a valid outline can supply deeper levels. Document-wide styles can also confuse callouts, captions and real section headings.
3. **Not every subsection is a chunk boundary.** Top-level splitting, plus second-level outline splitting, can still mix deeper sibling subsections. The existing overlap policy is useful, but not a complete hierarchical chunking design.
4. **PDF embedding paths intentionally use only the deepest heading.** `passage_text` documents why: incorrect ancestors polluted previous retrieval. Full paths should be introduced only for validated hierarchy, not enabled globally just because a new parser emits headings.
5. **Provenance can be approximate.** `_laid_out` assigns OCR transcript elements evenly spaced synthetic positions in a middle page band. Those positions aid ordering; they are not measured OCR boxes and must never be exposed as precise source highlights. Continued tables also need explicit provenance for every contributing page rather than a single original box.
6. **List and cell semantics are incomplete.** Current PDF text retains some bullet characters but has no explicit list tree; tables use header/row tuples and Markdown rather than a persistent cell graph with row/column spans and cell-level provenance.
7. **Revision metadata is not a complete revision workflow.** Documents start at revision 1; the handover lists later revisions as incomplete. Reindex replaces chunks with new records, which makes long-lived chunk references/version attribution an explicit migration concern.
8. **Quality warnings are not a first-class durable artifact.** There are validation/failure paths and local table rejection details, but no complete per-element audit trail exposing classification reasons, coverage and uncertainty to a future reviewer.

The appropriate integration point is inside ai-core's existing prepare/parse stage, before chunk creation and embedding. Content remains the owner that durably saves an accepted representation and serves authorized retrieval.

## 3. Representative PDF inspection

The PDFs are outside the repository, at `C:\Users\Naren\Documents\PROJECTSS\fsd_test\data\test_pdfs`. [Corpus manifest](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/scripts/eval/corpus.yaml) references **22 PDFs / 487 pages**, confirmed by opening them locally. Recursive directory inventory finds **24 PDFs / 490 pages**: the two extras are `gen-C1 Project Rubric.pdf` and `gen-C1 Testing Quiz Marking Scheme.pdf`. Keep those teacher-only documents as isolation fixtures, not student retrieval material.

This was a targeted inspection, not a full annotation or parser benchmark. Four pages were rendered together in memory and visually inspected:

| Document and physical PDF page | Observed layout | Benchmark use |
|---|---|---|
| `09-Security.pdf`, p16 | Two bullet groups, password/salt flow with arrows and small image text, plus a comparison table. Native text contains 588 characters but cannot alone express all visual relationships. | Separate figure, table and prose coverage; preserve arrow semantics and source kinds. |
| `12-Automation and Deployment.pdf`, p9 | Four vertical responsibility columns and a blue/white user/provider legend. Native text contains 566 characters; labels alone omit the color relationship. | Table/diagram interpretation, cell/legend association and accurate IaaS ownership answer. |
| `pure_pdfs/2103.00020v1.pdf`, p2 | CLIP paper: wide figure and caption above two text columns, with running title/page number. | Reading order, spanning figure/caption and furniture exclusion. |
| `pure_pdfs/NIST.AI.100-1.pdf`, p13 | Running headers, paragraphs, numbered heading `1.2.4`, printed footer `Page 8`. Heading number and body both use approximately 11.955 pt; heading is bold. | Equal-size typography, numbering and nested outline; distinguish physical p13 from printed p8. |

Additional native inspection: the security and deployment decks have flat one-entry-per-slide outlines; NIST has 30 bookmark entries across three levels; Attention Is All You Need has 22 entries across three levels. `gen-Scanned Sprint Planning.pdf` has seven pages and no bookmarks; its first page has one image and **zero native text characters**. A separate extraction/OCR route is necessary for that page. Metadata can be misleading: both inspected slide decks report title `Slide 1`.

The known `figure-password-salt-flow` and `tab-iaas-os-owner` kind failures are described in [RAG quality follow-up](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/docs/development/rag-quality-followup.md). Correct-page retrieval does not prove structural extraction is correct. Existing retrieval results (hit@1 0.924, MRR 0.950, zero detected leaks in that recorded run) are historical baselines, not measurements of any proposed parser.

## 4. Technical comparison

The matrix describes documented features and engineering expectations, **not measured rankings**. “Good candidate” means worth testing; it does not assert accuracy on these PDFs. A document representation supporting hierarchy does not mean the PDF parser correctly inferred that hierarchy.

### Fidelity and provenance

| Approach | Native text | Headings and hierarchy | Tables | Reading order | Page/box provenance and RAG implications |
|---|---|---|---|---|---|
| PyMuPDF plus existing rules | Direct span/word extraction avoids OCR errors on sound text layers; broken encodings remain possible | Size/weight/position and outlines are available; repository already infers levels. Ambiguous styles require more context | Native table finder and existing custom recovery; merged/borderless/semantic matrices remain hard | Sorting and custom region logic; current implementation covers two columns with spanning bands, not every layout | Strong raw coordinates; need to preserve them through normalization/chunks. Smallest integration change |
| Docling standard PDF pipeline | Combines PDF parsing with layout and optional OCR; preservation must be measured | Typed sections/groups; current documented PDF hierarchy recovery is opt-in, using bookmarks, numbering and styles | Specialized structure recognition is a strong candidate for complex tables, not proof of correct cell values | Dedicated layout/reading-order pipeline; benchmark mixed columns, slides and sidebars | Structured document with provenance and boxes where available; closest fit to a rich IR |
| PyMuPDF4LLM / current PyMuPDF layout exports | Text plus Markdown/JSON-oriented extraction | Heading helpers and layout classes; compare actual nested structure, not just `#` output | Convenient table serialization; verify spanning cells and provenance survive | Layout-aware output; evaluate columns and wide objects explicitly | Current documented exports include page/layout metadata. Markdown alone discards important relationships |
| Unstructured | `fast` for extractable text; `ocr_only` for scans; `hi_res` for modeled layout | Typed Title/NarrativeText/ListItem etc.; parent/category metadata is useful but not guaranteed true PDF heading depth | Documented PDF table extraction uses `hi_res`; HTML representation can carry structure | Official partitioning docs warn about multi-column ordering in `hi_res` | Page metadata and optional coordinate systems; adapter needed for a consistent tree |
| OpenCV plus OCR | Rasterizes readable text and then recognizes it; unnecessary error opportunity for clean native PDFs | Image zones and OCR are not heading semantics; hierarchy requires extra rules/models | Lines/contours can find grids; cell recognition, merges and borderless tables require further work | Must implement region ordering and joins | OCR boxes can be mapped back to PDF coordinates with explicit transforms; largest custom ownership burden |
| Routed hybrid | Preserve sound native text; escalate only unresolved pages/regions | Reconcile outlines, layout classification, numbering and typography with explicit uncertainty | Native cells first; layout/OCR for failures, vision for semantic relationships if authorized | Combine region order with document-wide structural validation | Best architectural fit if the router is accurate; disagreements and duplicated content need deliberate arbitration |

### Compute, dependencies, licenses and project fit

Relative resource descriptions below are **hypotheses to measure**, not latency/RAM guarantees.

| Approach | CPU/GPU and expected resource shape | Dependency/operational burden | Licensing evidence | Fit here |
|---|---|---|---|---|
| Existing PyMuPDF | CPU-native; no layout-model weights; rendering/OCR adds work | Already installed/pinned. Repository serializes PyMuPDF calls per process for safety | AGPL/commercial dual licensing documented by Artifex | Baseline and coordinate provider |
| Docling standard | CPU-capable; optional GPU acceleration. Layout/table/OCR inference adds model startup and memory; bound decoded pages and queues | New Python/runtime/model stack, approved prefetching, Python 3.13 and Linux wheel checks, worker sizing | Code MIT; individual model/package licenses must be checked separately | Leading structural challenger; avoid automatic VLM/remote enrichment |
| PyMuPDF4LLM/layout | Current documentation says no GPU requirement; CPU throughput and model/package footprint still need measurement | Closest ecosystem, but current docs describe newer PyMuPDF than this project uses; version trial needed | PyMuPDF4LLM repository contains AGPLv3; underlying PyMuPDF has commercial option | Lighter challenger, not a drop-in assumption |
| Unstructured | `fast` avoids layout inference; `hi_res` and OCR consume more compute; acceleration depends on selected model/runtime | PDF extras, inference runtime, PDF rendering/OCR/system packages; pin actual engine and strategy | Core code Apache-2.0; optional models/dependencies and hosted terms separate | Broad toolkit, but existing native-format support reduces its main convenience advantage |
| OpenCV + Tesseract | CPU sufficient for basic preprocessing/OCR; no GPU needed for thresholding or standard Tesseract | Rasterization, language packs, transforms, segmentation and substantial custom maintenance | Current OpenCV and Tesseract repositories use Apache-2.0 | Targeted scan cleanup, not default extraction or custom model training |
| Hybrid | Cheap native route plus costlier escalations; tail latency depends on escalation frequency | Multiple adapters/router; requires quality telemetry, reproducible versions and failure limits | Aggregate licenses of enabled components | Recommended direction, conditional on benchmark quality and maintainability |

Verified references supporting the matrix:

- [PyMuPDF text extraction](https://pymupdf.readthedocs.io/en/latest/recipes-text.html) documents position-aware text extraction, ordering and table-related workflows. Its primitives provide observations from which a hierarchy can be inferred; they do not solve every semantic ambiguity.
- [PyMuPDF licensing](https://pymupdf.readthedocs.io/en/latest/about.html) documents AGPL/commercial availability. This applies to a dependency already in the project, rather than a new concern introduced solely by this proposal. This report records license facts, not a legal conclusion about the application's obligations.
- [Docling document model](https://docling-project.github.io/docling/concepts/docling_document/) supports text, tables, pictures, sections/groups, body/furniture distinctions and provenance/boxes where available.
- [Docling PDF heading levels](https://docling-project.github.io/docling/usage/heading_levels/) says layout detection alone produces flat section headers. Heading-level recovery is disabled by default; it can use bookmarks, numbering and visual styles. The documented configuration enables `HeadingHierarchyOptions(enabled=True)` and retains parsed pages for style signals. These options must exist in the selected pinned release.
- [Docling advanced options](https://docling-project.github.io/docling/usage/advanced_options/) covers offline model artifacts, opt-in remote services, table configuration and bounded resource queues. [GPU support](https://docling-project.github.io/docling/usage/gpu/) documents accelerator choices and CPU-only benchmark configurations. Published timings are hardware/configuration-specific and were not transferred into this project's estimates.
- [Docling README/license](https://github.com/docling-project/docling#license) explicitly separates MIT code licensing from individual model licenses. No blanket assumption is made about an OCR or VLM checkpoint.
- [PyMuPDF4LLM overview](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/) describes Markdown, JSON, TXT and CPU layout support. [Current API](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/api.html) now documents core functionality moving to `Document` in PyMuPDF 2.0, with compatibility wrappers, heading helpers and page layout classes. **The repository is on 1.28.2**: do not copy these current APIs into it without a version-specific trial. [PyMuPDF4LLM license](https://github.com/pymupdf/pymupdf4llm/blob/main/LICENSE).
- [Unstructured partitioning](https://docs.unstructured.io/open-source/core-functionality/partitioning) documents `auto`, `fast`, `hi_res`, `ocr_only`, strategy fallback, table inference and multi-column limitations. Its model names/examples are not a substitute for verifying the installed release. [Element metadata](https://docs.unstructured.io/open-source/concepts/document-elements) describes IDs, parent metadata and optional coordinates with coordinate systems. [Core license](https://github.com/Unstructured-IO/unstructured/blob/main/LICENSE.md).
- [OpenCV thresholding](https://docs.opencv.org/4.13.0/d7/d4d/tutorial_py_thresholding.html) provides preprocessing techniques, not a document hierarchy model. [Tesseract quality guidance](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html) discusses resolution, binarization, deskewing and segmentation, and cautions that retraining is usually not the first remedy. [OpenCV license](https://github.com/opencv/opencv/blob/4.x/LICENSE), [Tesseract license](https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE).

### Why native extraction remains useful

For digitally generated slides and reports with good text layers, use native text as the transcription authority and layout as an organizing signal. OCR cannot recover the author's semantic intent merely by rereading the same visible characters, and can corrupt code, punctuation and numbers. OCR is appropriate for image-only pages, image-only regions or unusable encodings. A PDF with a hidden OCR layer needs quality assessment: an invisible text layer may be useful, stale or wrong; visibility alone should not decide its reliability.

Vision adds value for arrows, color legends, charts and photographed tables. It is not required for every paragraph or heading. Keep model-written descriptions separate from native quotations and preserve the image evidence. A cloud-service responsibility diagram can be both visually a figure and semantically a matrix: record a primary type plus relationships/derived table view rather than forcing one global type to erase the other.

## 5. Reconstructing headings and hierarchy

Proposed method, shared across parser adapters:

1. Collect page geometry, word/spans, font family/size/weight/slant, line gaps, indentation, alignment, numbering and bookmark destinations. Inspect tagged-PDF structure when available through the chosen backend; none of the compared configurations has been verified here to recover every tagged structure correctly.
2. Estimate body styles per document and layout region, weighted by characters, excluding furniture and obvious tables. A single document-wide most-common font can misclassify mixed appendices, code, title pages and slides.
3. Score heading candidates using relative size, weight, whitespace above/below, shortness, left alignment, numbering consistency, neighboring prose and recurring templates. Keep the features/reasons with the decision. A rule score is not a calibrated probability.
4. Match bookmarks to actual text and position; reject stale/duplicate matches. Then use coherent numbering and document-level style clusters for depth. Bookmarks are evidence, not an excuse to accept wrong destinations.
5. Build a stack of section nodes, attaching paragraph/list/table/figure nodes in reading order. Preserve unresolved candidates with `heading_level: null`; do not invent parent sections or missing heading text to make the tree look complete.
6. Validate globally: numbering progression, plausible style-depth patterns, unresolved jumps, excessive one-line sections, headings inside table cells and repeated running text. For slides, the physical slide is an explicit unit even when the deck outline is flat.

**Same font and size:** numbering, whitespace, indentation, bookmark matches and repeated document patterns may still separate headings from body. Same font, size and weight with no numbering, tags or outline can be genuinely ambiguous. Layout models can help classify regions but cannot guarantee the intended hierarchy. Prefer a shallow, evidence-backed tree with warnings over confident invented nesting. Manual labels are necessary to evaluate these cases.

Furniture should be retained in the audit representation with an excluded-from-retrieval role. Match repeated text at consistent margin positions across enough pages, allowing page digits to vary; protect recurring section titles, table headers and short documents. Keep physical PDF page number, PDF page label and printed page text as separate fields. Treat title metadata as a candidate: `Slide 1` should not overwrite a teacher's chosen title or a clearly printed document title.

## 6. Proposed ingestion architecture

No extra business application, vector database or agent framework is proposed.

```text
Teacher -> Content upload, authorization, original object + SHA, durable index job
                     |
                     v
              ai-core /v1/ingest
                     |
         validate and inspect original
                     |
        native PDF text/geometry inventory
                     |
      parser adapter / explicit quality route
        | native | Docling challenger | OCR for missing text |
                     |
      normalize -> validate structured document JSON
                     |
      section/element-aware token chunking
                     |
      ai-core budgeted embeddings / exact-input cache
                     |
         return accepted artifact + chunks/vectors
                     |
     Content stores immutable artifact in its object store;
     transaction publishes active index generation + chunks + outbox event
                     |
     authorized Content search -> rerank -> bounded section expansion
                     |
             chatbot / teacher generation -> page-grounded citations
```

Adapter contract should return the same document model plus capability flags and warnings, independently of the vendor/library. Separate extraction, normalization, hierarchy validation, chunking and embedding versions. An extraction-only proof of concept should stop before embeddings; `/v1/ingest` currently bundles these stages, so do not call it casually for a supposedly free parser experiment.

Start with one baseline adapter and one challenger, not six production backends. Record route reasons such as missing usable text, unresolved column order or inconsistent table grid. Do not escalate a page merely because a heading uses a small font. Cross-parser reconciliation should assign each native span/region to at most one final content element; retain conflicting candidates in diagnostics. Page-level escalation may be easier to reproduce than region-level assembly initially, while document-wide context remains necessary for headings.

### Storage, authorization and lifecycle

Proposed durable ownership:

- **Content object storage:** original PDF, immutable structured JSON, optional Markdown view and extracted crops, within the document's existing protected prefix.
- **Content PostgreSQL:** active extraction/index manifest with schema/parser/config/model versions, hashes, warning summary and artifact key; section/element lookup metadata where needed; chunk-to-element links and pgvector/full-text indexes. Full style/span JSON need not inflate every hot search row.
- **ai-core:** bounded operational parse/model/embedding caches; no authoritative document business records. Cache keys include file hash, parser/model/checkpoint, normalization/hierarchy/schema versions and extraction settings. Chunking and embedding versions should be separate so rechunking can reuse extraction.

Content writes the immutable object before atomically switching the active manifest and chunks under the lease/revision check. A failed SQL commit may leave an unreferenced object, which must use the existing durable cleanup/sweep mechanism. Readers see a complete old or complete new generation. Failed regeneration should retain the prior accepted generation rather than publish half a replacement. This staging behavior requires design and additive migrations; it is not claimed as current behavior.

Every parent/neighbor lookup must reapply document authorization, org, classroom and teacher/student document-type rules. Parser caching must not expose cache existence or share business retrieval rights. Continue all paid OCR/VLM/embedding/rerank work through ai-core's existing billed-user reservation/accounting path. No remote library feature may make an unmetered provider call.

### Errors and confidence

Validate limits before expensive work: bytes, page count, decoded pixels, execution time, output size, elements, cells and chunks. Keep the existing PyMuPDF serialization boundary; future parallel parsing should use safe isolated processes with bounded concurrency rather than remove the lock. Prevent new layout/OCR threads from starving online inference.

Permanent corrupt/encrypted/oversize failures remain explicit. Retry transport/model outages with existing backoff; do not cache a partial response as a successful parse. Distinguish usable-with-warnings, incomplete extraction and fatal failure in the future contract. Never silently drop bad pages or report complete indexing for a known incomplete artifact. Low-confidence material may require review or remain unavailable for retrieval; adding a review state/UI is a proposed, approval-dependent product change.

Missing boxes remain `null`. Synthetic OCR placement is marked estimated, not measured. Record coverage discrepancies and the reason a body region was excluded. Treat all extracted text, OCR and figure instructions as untrusted content; the existing injection fixture must remain a security test.

## 7. Structured document schema

Proposed schema principles:

- A **source revision** identifies original bytes; an **extraction** identifies a parser/config/model/normalizer run; an **index generation** identifies chunking/embedding choices. A new parser does not imply a new teacher-authored source revision.
- An element ID is scoped to an extraction and linked to stable source anchors. Deterministic IDs can hash extraction identity plus page/span/type/ordinal. Text alone is insufficient because identical bullets recur. Cross-revision element matching is a separate best-effort relation, never guaranteed ID stability.
- A document is a tree for containment and a graph for references, continuations and caption relationships. Pages are provenance, not mandatory parents: a section/table can span pages.
- Geometry uses canonical upright page coordinates in PDF points, top-left origin; retain original crop/rotation transforms, dimensions and all contributing boxes. Separate physical page number from display/printed labels. Character ranges explicitly use Unicode code points in normalized element text.
- Typed table cells retain row/column index, spans, header role, text and provenance. Lists retain ordered/unordered markers, numbering and nesting. Figures retain crop references, caption links, native labels and separately marked generated descriptions. Document metadata retains both original and selected values with source attribution.

Illustrative JSON below is **invented schema example data, not output extracted from the user's PDFs or measured confidence**. Names, content and coordinates only demonstrate relationships.

```json
{
  "schema_version": "structured-document.v1",
  "document_id": "example-doc",
  "source_revision": 1,
  "source_sha256": "example-source-hash",
  "extraction_id": "example-extraction",
  "parser": {"name": "example-adapter", "version": "pinned-version", "config_sha256": "example-config-hash", "models": []},
  "normalization_version": "example-normalizer.v1",
  "metadata": {"teacher_title": "Example deployment notes", "pdf_title": null, "language": "en"},
  "pages": [{"number": 1, "pdf_label": "1", "printed_label": null, "width_pt": 612, "height_pt": 792, "rotation_degrees": 0}],
  "root_id": "root",
  "elements": [
    {"id": "root", "type": "document", "parent_id": null, "children": ["s1"], "provenance": []},
    {"id": "s1", "type": "section", "parent_id": "root", "children": ["h1", "s2"], "heading_id": "h1", "heading_level": 1, "provenance": []},
    {"id": "h1", "type": "heading", "parent_id": "s1", "children": [], "text": "Deployment", "reading_order": 0,
     "provenance": [{"page": 1, "bbox_pt": [48, 72, 220, 94], "coordinate_space": "upright-page-top-left", "geometry": "measured", "text_method": "native", "char_range": [0, 10]}]},
    {"id": "s2", "type": "section", "parent_id": "s1", "children": ["h2", "p1", "l1", "t1", "f1", "c1"], "heading_id": "h2", "heading_level": 2, "provenance": []},
    {"id": "h2", "type": "heading", "parent_id": "s2", "children": [], "text": "Ownership", "reading_order": 1, "provenance": []},
    {"id": "p1", "type": "paragraph", "parent_id": "s2", "children": [], "text": "Ownership differs by service model.", "reading_order": 2, "provenance": []},
    {"id": "l1", "type": "list", "parent_id": "s2", "children": ["li1"], "ordered": false, "reading_order": 3, "provenance": []},
    {"id": "li1", "type": "list_item", "parent_id": "l1", "children": [], "marker": "bullet", "text": "Check operating-system responsibility.", "provenance": []},
    {"id": "t1", "type": "table", "parent_id": "s2", "children": [], "reading_order": 4, "rows": 2, "columns": 2,
     "cells": [
       {"row": 0, "column": 0, "row_span": 1, "column_span": 1, "header": true, "text": "Model", "provenance": []},
       {"row": 0, "column": 1, "row_span": 1, "column_span": 1, "header": true, "text": "OS owner", "provenance": []},
       {"row": 1, "column": 0, "row_span": 1, "column_span": 1, "header": false, "text": "IaaS", "provenance": []},
       {"row": 1, "column": 1, "row_span": 1, "column_span": 1, "header": false, "text": "User", "provenance": []}
     ], "provenance": []},
    {"id": "f1", "type": "figure", "parent_id": "s2", "children": [], "reading_order": 5, "crop_key": "example-figure.png", "native_labels": [], "generated_description": null, "provenance": []},
    {"id": "c1", "type": "caption", "parent_id": "s2", "children": [], "text": "Figure 1. Service responsibility.", "reading_order": 6, "provenance": []}
  ],
  "relations": [{"type": "caption_of", "from": "c1", "to": "f1"}],
  "quality": {"status": "example-only", "warnings": [{"code": "EXAMPLE_INCOMPLETE_PROVENANCE", "element_ids": ["h2", "p1", "l1", "li1", "t1", "f1", "c1"]}]}
}
```

Empty provenance above keeps the example readable; **real accepted content must have page attribution**, and absent boxes require an explicit reason/quality warning. Heading levels can be null when unresolved. Optional classification metadata should include method, evidence features, raw model confidence if supplied, calibration version if available, and uncertainty reasons; do not assign fabricated numeric confidence.

Illustrative chunk record, stored separately from the source tree:

```json
{
  "chunk_id": "example-chunk",
  "document_id": "example-doc",
  "source_revision": 1,
  "extraction_id": "example-extraction",
  "index_generation_id": "example-index",
  "parent_section_id": "s2",
  "element_ids": ["p1", "li1"],
  "element_ranges": [{"element_id": "p1", "start": 0, "end": 35}, {"element_id": "li1", "start": 0, "end": 38}],
  "heading_path": ["Deployment", "Ownership"],
  "page_start": 1,
  "page_end": 1,
  "kind": "text",
  "text_method": "native",
  "chunker_version": "example-chunker.v1",
  "embedding_model": "configured-model",
  "embedding_input_sha256": "example-input-hash"
}
```

Ranges must equal the actual selected text slices; the example uses end-exclusive Unicode code-point offsets. Keep existing UUID API IDs or explicitly version contracts rather than substituting the readable example IDs. Bounding boxes for a multi-element chunk come from its element/range provenance, not a fabricated enclosing box across unrelated pages.

### Updates and duplicates

Treat `(authorized document/source revision, extraction config)` as an idempotent ingestion unit. A repeated byte-identical upload can reuse safe computation without merging its classroom/owner permissions. A revised file gets a new immutable source revision; unchanged elements may be matched by content plus layout/context for reuse, but the revision still has distinct provenance.

Separate embedding cache identity from element identity: reuse exact assembled input/model vectors even when an element's page moves. If a heading changes, its contextualized embedding input changes too. Cache invalidation must include parser/hierarchy/normalization settings; old extraction artifacts remain immutable. Citation snapshots need revision/extraction identity so reindexing cannot silently redirect an old answer to changed material. Deletion and classroom purge must cover structured artifacts and lookup rows through Content's existing cleanup/outbox patterns.

## 8. RAG integration design

Use the structured JSON as the source of truth. Generate Markdown for display, debugging and embedding serialization. Markdown is useful, but ordinary Markdown does not preserve precise boxes, merged cell spans, typed references, confidence or every page break.

Proposed chunking policy:

1. Form sections and typed elements before token packing. Use actual slide boundaries for decks; allow prose sections to continue over physical pages.
2. Default child chunks to the current **600-token / 90-token overlap** baseline initially. Compare alternatives later, with the same tokenizer and full contextual-input budget. Do not tune parser, chunk size and rerank floor simultaneously.
3. Split at validated subsection boundaries; allow a parent title to introduce its child. Overlap only within the same semantic section. Lists keep their introducer and nesting; oversized lists split at item boundaries with necessary ancestor markers.
4. Keep tables separate from prose. Split large tables by rows while repeating actual headers and caption; retain table/cell IDs and spans. Oversized cells require explicit fragments linked to the same cell, not misleading partial rows. Keep the existing column-labelled embedding serialization as a baseline.
5. Keep figure/caption/native-label links. Store inferred descriptions as generated evidence with model/version provenance. Do not pretend generated words are verbatim PDF quotations.
6. Assemble embeddings from teacher/document title, a **validated** relevant heading path and child content. Budget the whole input, bound repetitive ancestors, and compare leaf-only versus full-path variants. No LLM-generated contextual summaries are needed initially.
7. Retrieve child chunks through existing keyword/vector/RRF/rerank. Collapse duplicate overlap by element ranges. Only then expand to a bounded neighboring chunk or parent section through an authorized Content API. Prefer retrieval of the smallest section supplying missing context; do not automatically attach whole chapters.
8. Preserve element-level page references in the answer context so a parent spanning several pages cannot yield a guessed page citation. Expand figures/tables through explicit links rather than only keyword coincidence.

Useful future metadata filters include source revision, active index generation, element kind, section ID and extraction quality. These would be additive authorized Content API features; topic filtering is not currently equivalent to the eval corpus's topic IDs, as the existing skipped case records. No agent may directly query Content's database to implement expansion.

Docling's [chunking documentation](https://docling-project.github.io/docling/concepts/chunking/) provides hierarchical/token-aware chunkers and metadata contextualization. Benchmark them as an alternative serializer; preserve the project's citation, table, figure and budget contracts instead of automatically replacing its chunker or adopting another agent framework.

## 9. Reproducible evaluation plan

No evaluation described here has been executed. Existing sample extraction/render inspection is not a benchmark result. Keep evaluation offline and extraction-only first; existing golden RAG tests validate downstream behavior but do not supply heading/tree/cell ground truth.

### Corpus, labels and splits

Start with 12 documents spanning real lecture decks, scanned slides, NIST, Attention, CLIP, the RAG paper, generated notes, the revised collaboration document and the injection/photo fixtures. Annotate approximately 50 selected pages for the first proof of concept, including the four rendered pages, scanned pages, nested NIST headings, tables, contents and first/last pages. Parse whole documents even when scoring selected pages so heading-style and furniture inference sees document context.

Stratify into clean native prose, multi-column prose, slides/lists, complex tables/diagrams, scans/mixed regions and adversarial text. Keep revised/duplicate document families in the same split. The existing golden holdout has already been inspected in previous work; retain it for regression comparison but create a fresh locked structural holdout for an honest new generalization check.

Manual annotation needs exact text, heading spans/levels/parents, paragraph/list groupings, table cells/spans/header links, captions/references, reading-order precedence, furniture and physical page IDs/boxes. Record ambiguous labels and permitted alternatives. Double-annotate a difficult subset and adjudicate disagreements before using it to rank parsers.

The 22-file corpus does not establish broad language/layout coverage. Later collect permitted samples with image-only prose, mixed hidden OCR, skew/rotation, three columns, sidebars, equal-font/equal-weight headings, broken encodings, merged multi-page tables, footnotes/equations, tagged versus untagged PDFs and malformed/encrypted files. Never add answer keys to a student's corpus to improve benchmark coverage.

### Measurements and checks

| Failure or goal | Reproducible measurement | Ground truth / automation boundary |
|---|---|---|
| Missing/incorrect text and OCR errors | CER = character edits / reference characters; WER = word edits / reference words. Also missing/extra token rates, duplicate-region/text rate and exact numeric/code-token accuracy | Requires annotated transcription. Run raw and normalized variants with published dehyphenation/ligature rules; don't normalize away meaningful errors |
| Heading classification | Span-matched precision/recall/F1 with fixed text/box overlap matching; macro F1 across layout strata | Annotated heading spans; freeze matching thresholds on tuning data |
| Nesting/hierarchy | Heading-depth accuracy, parent-edge F1 and normalized tree-edit distance after node alignment; document-level complete-tree correctness | Annotated tree; do not score only headings a parser happened to find |
| Lost/merged paragraphs and lists | Paragraph alignment coverage, boundary F1, list-item/nesting/marker accuracy | Annotated blocks and list relationships |
| Multi-column order | Pairwise precedence accuracy / inversion rate over matched blocks; measure text coverage separately | Annotated partial reading order; alternative valid ordering should not be penalized |
| Tables | Cell text accuracy, row/column assignment F1, header association F1, merged-span accuracy; optional TEDS on canonical HTML | Annotated cell grid/HTML, including row/column spans; page/table detection alone is insufficient |
| Headers/footers | Furniture precision/recall; body-text deletion rate; retrieval tokens wasted on retained furniture | Annotated roles; catches overly aggressive repeated-title deletion |
| Page/location accuracy | Physical-page exact match and box IoU for matched elements; measured versus estimated box coverage | Page attribution can be checked mechanically; correctness needs reference locations |
| Figures/captions/references | Caption/link precision/recall; native-label recall; diagram relation/value accuracy and unsupported generated claims | Human annotation and visual review for arrows, legends and descriptions |
| Chunks crossing sections | Fraction of chunks containing unrelated leaf-section elements; missing-element coverage; overlap duplication; full-input token distribution | Automatically computed from aligned annotated tree; requires semantic boundary labels first |
| Update/idempotency | Same inputs/config produce same normalized IR hashes; duplicate jobs don't publish two active generations; old citation/version still resolves; failed update retains old generation | Future deterministic/integration fixtures; test cache cold/warm and deletion races |
| Runtime/cost | Per-document/page time; median and tail latency; peak process-tree/container memory, CPU time, image/model disk footprint, startup time, escalation/OCR rates and paid tokens/USD | Automated instrumented runs; include child processes, timeouts, failures and model loading |
| RAG quality/security | Existing hit@1/3/5, MRR, recall@5/10, kind failures, refusal behavior, citation validity, unanswerable and leak tests | Existing golden labels plus manual answer review; retrieval hit is not proof of supported answer |

For table metric methodology, [PubTabNet / TEDS](https://github.com/ibm-aur-nlp/PubTabNet) is a primary technical reference; do not compare parser-specific Markdown strings as if they were equivalent cell structures.

### Experiment controls

For each adapter, pin package versions, native backend, model checkpoint hashes, language/OCR choices, heading options and rendering DPI. Record source-file hashes, OS, CPU/GPU, RAM/container limits, threads, batch size, concurrency, cache state and seeds where supported. Run one worker first; compare cold startup separately from warmed repeated runs. Use at least three measured repetitions once the harness is approved, with identical inputs and resource limits. Report failures rather than excluding slow/bad documents. Do not compare a fresh model download against another parser's warm cache.

Compare in stages: (A) existing baseline, (B) baseline plus persistent IR/validated heading rules, (C) Docling standard with appropriate OCR and explicit hierarchy, (D) version-compatible PyMuPDF4LLM, (E) Unstructured explicit fast/hi_res variants, (F) scan preprocessing/OCR where needed. Avoid letting `auto` silently select different strategies without recording them. A hybrid router must also be tested on the pages it sends down the cheap path: false negatives can hide its worst structural errors.

Select extraction/hierarchy first with the current chunk settings frozen; then compare chunkers on the selected IR; then run downstream retrieval/answer evaluations with fixed models/prompts/rerank threshold. Current scripts are [retrieval golden](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/scripts/eval/run_golden.py) and [chat evaluation](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/scripts/eval/run_chat.py). They can invoke paid endpoints. The earlier instruction to defer tests remains in force, and the extraction research has not run them.

Proposed acceptance criteria for review, **not established performance**: zero authorization leaks and no false success for incomplete documents; no page-attribution regressions; no new silent paragraph/cell loss; improved heading-parent F1 and table association on the fresh structural holdout; no material retrieval/citation regression in each layout stratum; and the known salt-flow/IaaS cases resolved with visual evidence. Set numeric accuracy and local latency/RAM thresholds after annotation/baseline, rather than inventing an arbitrary production SLA. Report paired document-level differences and uncertainty intervals; averages must not conceal a scan or table regression.

Do not start a full paid reindex/eval under the assumption it fits $5. First measure free extraction and count proposed embedding/vision inputs, then inspect existing development/test charges and outstanding reservations. Later paid trials require a remaining-budget allocation and the existing ai-core cap, with both ledgers preserved.

## 10. Incremental roadmap — approval required before implementation

| Phase | Work after explicit approval | Exit evidence |
|---|---|---|
| 0. Agree benchmark and representation | Select pages/labels, define canonical coordinate and version semantics, choose resource envelope | Frozen corpus hashes, annotation guide and schema review |
| 1. Minimal offline proof of concept | Use existing PyMuPDF only; export typed elements/tree/real page boxes from a separate extraction-only harness. No app route, migration, paid embedding or production reindex | Inspectable JSON/Markdown and page overlays; baseline fidelity/resource report |
| 2. Controlled challenger trial | Approve isolated dependency/model downloads; trial Docling standard with hierarchy enabled and PyMuPDF4LLM with compatible pins; Unstructured if it adds a plausible advantage | Same corpus/labels/limits, per-stratum comparison, license/model manifest, install compatibility results |
| 3. Choose primary parser/router | Fix classification/provenance gaps, benchmark escalation decisions and failure paths; keep optional OCR/vision separate | Selected configuration wins on structural evidence; no silent missing regions; bounded compute |
| 4. Product integration | ADR amendment/new ADR as appropriate, additive Content manifest/element/chunk links, private ingest schema, immutable artifacts and atomic generation publication | Contract checks and local/infrastructure tests, deletion/update/auth replay behavior |
| 5. Hierarchical retrieval trial | Section-safe chunks, validated embedding headings, authorized bounded neighbor/parent expansion | Offline chunk metrics, then budget-approved targeted retrieval/chat runs |
| 6. Controlled rollout | Explicit reindex selection, versioned caches, progress/warnings and rollback to prior accepted generation | Fresh holdout and source-citation review; measured resource/cost limits, no production-readiness claim |

When implementation is approved, run `scripts/check.py` and applicable real database/integration/E2E suites as required by repository boundaries, reporting anything unavailable. Existing native-format tests/deployment remain a separate outstanding validation item. This research does not implicitly approve those runs or container changes.

## 11. Open questions and risks

1. Is the long-term corpus mostly course slides or mixed reports/scans? The observed corpus supports both; their structure policies differ.
2. What hardware and container memory limits must ingestion share with online AI? CPU viability is documented, but acceptable local latency and contention are unmeasured.
3. Which pinned Docling/PyMuPDF4LLM releases expose the documented hierarchy/layout APIs and support this Python 3.13 lockfile? Current web docs can lead the deployed dependency versions.
4. Are English-only OCR and diagrams sufficient? Language and equation requirements affect models, font handling and labels.
5. How should low-confidence content appear to teachers? Review/partial status needs an explicit product/contract decision; existing `indexed` cannot quietly mean incomplete.
6. Should semantic matrices be searchable as table views while retaining a figure source? The IaaS slide exposes this difference between a visual type and a retrieval need.
7. What is the document replacement/version-retention policy? Durable historical citations, deletion, storage growth and teacher edits must be designed together.
8. Package code licenses are verified; the exact model/dependency license set remains unknown until versions/checkpoints are selected. No new commercial license price was assumed.
9. Finer source locations can be wrong after normalization, rotation or OCR. Keep source/normalized text alignment, explicit transforms and estimated/missing geometry flags.
10. Native extraction quality and structural RAG quality are related but distinct. Better hierarchy can still hurt retrieval if wrong ancestor headings dominate embeddings or parent expansion exhausts context.

The initial research recommendation was to approve phases 0–1: a local, extraction-only PyMuPDF proof of concept and annotated benchmark. That makes existing losses visible before spending on a new parser or reindexing the corpus. The research itself did not implement a pipeline; the subsequent authorization and limited implementation are recorded below.

## 12. Implementation checkpoint after owner authorization

The owner subsequently asked to try implementation. The first offline phase now has a standalone [CLI](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/pdf_structure_probe.py), [fixture tests](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/tests/test_pdf_structure_probe.py) and a [proposed 50-page annotation selection](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/scripts/eval/structure_cases.json). This does not activate a new production parser.

From the repository root in PowerShell, using the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe services\ai-core\pdf_structure_probe.py "..\fsd_test\data\test_pdfs\09-Security.pdf" --output .local\pdf-structure-first
```

The CLI parses the complete document for style/bookmark context and creates a new document/hash directory containing:

- `structure.json`: experimental tree, native text/font spans, physical pages, measured source boxes, candidate headings/furniture, image regions, ruled-table cells and warnings.
- `review.md`: readable text and element/page/box anchors for manual comparison against the original PDF.
- `annotations.json`: **empty, unannotated** ground-truth workspace. Parser predictions are not copied in as gold labels.

Reruns refuse to overwrite the same document's directory, protecting any annotations. Choose a new `--output` directory for another run. The CLI accepts multiple input paths, but does not automatically load the selection manifest. The manifest proposes pages to review, not pages to extract; extract the whole corresponding document. The 50 selected page numbers were checked against the actual PDFs. Original-file hashes are recorded at extraction time; the corpus is not yet a frozen, adjudicated benchmark.

This prototype deliberately retains text at **line level with source block IDs**, rather than claiming reliable paragraph or nested-list reconstruction. Numbering, exact same-page bookmark matches and relative size/boldness provide heading candidates; they are not the running application's existing parser or a demonstrated accuracy improvement. Native text and table views coexist to avoid dropping text prematurely; table views are explicitly marked as alternate representations and must not both be embedded. Images have measured regions but no generated semantics, vector-only figure detection or caption linking. Scan pages emit explicit no-OCR warnings and never fabricated text.

For geometry, the prototype uses `pymupdf-unrotated-page` coordinates and records crop boxes, rotation and rotation matrices. It has not yet normalized every backend to the proposed canonical upright coordinate schema. Section nodes reference their headings; leaf elements own measured locations. Reading order remains PyMuPDF's sorted order, **not validated two-column reconstruction**. Native image bytes are excluded from text-dictionary extraction to avoid unnecessarily decoding them. File, page, native text, element and table-cell limits bound the experiment; a production time/memory sandbox is not provided by this local CLI.

Static verification: Ruff format/lint and mypy passed for the new CLI; Ruff also checked the new test source. Runtime extraction and fixture execution have **not** been performed in this implementation turn, preserving the owner's request to run tests later. When ready, the focused fixtures can be run separately without providers or a stack:

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m pytest -c services/ai-core/pyproject.toml services/ai-core/tests/test_pdf_structure_probe.py
```

Full `scripts/check.py`, real infrastructure integration/E2E and paid golden runs remain deferred. The app, existing PDF ingestion, credentials, stored documents and spend ledger are unchanged. No new dependency/model installation or paid call occurred. The next technical work is to inspect exported artifacts, annotate representative pages and measure structural errors before choosing any new parser; Docling integration and production storage/retrieval changes remain later phases.
