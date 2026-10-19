"""Ingest: one PDF → embedded chunks and the figure crops they point at. Ported from fsd_test's knowledge service.

ai-core is stateless (ADR 0002): Content sends the PDF and stores what comes back (chunks, vectors, crops) in its
own database and bucket. Nothing here reads or writes business data; the only disk state is the vision cache,
model answers keyed by page content (ingest.vision), which makes a reindex free.

Tables: the vision pass (ingest.vision) looks at every page first, then the parser reads the tables it found from
the text layer. A vision failure only costs that page its tables.

Figures: the vision pass also describes the informative figures. Their crops come back named by content hash, so
a reindex never renames an image the live chunks still point at.

Scans: a page without a text layer is transcribed by the vision pass (OCR); its text chunks are source "vision".
NO_TEXT_LAYER is left for scans with OCR off, or whose transcripts come back empty.

Size: a PDF over INGEST_MAX_PAGES pages fails TOO_LARGE before any model call, and one over INGEST_MAX_CHUNKS chunks
before its embeddings, so one upload cannot spend a teacher's whole allowance.
"""

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from learning_transport.metrics import increment

import local_cache
import openai_client
import pdf
from ingest import costs, rescue, reuse, vision
from ingest.chunker import PATH_SEPARATOR, Chunk, chunk_paras
from ingest.headings import outline
from ingest.parse import Document, IngestError, open_checked, read_pages
from ingest.select import select_pages
from settings import Settings, get_settings

log = logging.getLogger("ai-core.ingest")

__all__ = ["IngestError", "IngestResult", "VisionConfig", "ingest", "passage_text", "vision_config"]


@dataclass(frozen=True)
class VisionConfig:
    model: str
    cache_dir: Path
    max_pages: int
    figure_model: str | None = None  # None: figures aren't described, so not indexed
    ocr_model: str | None = None  # None: pages without a text layer aren't transcribed
    # lays out tables no method could read (ingest.rescue); None (tests only: the service always sets it): they
    # stay prose
    table_model: str | None = None
    selective: bool = False  # explicit test/legacy configs retain the full pass


def vision_config(settings: Settings) -> VisionConfig | None:
    """The vision pass's settings, or None when TABLE_VISION is off (tables then come from "lines" only)."""
    if not settings.table_vision:
        return None
    return VisionConfig(
        settings.table_vision_model,
        settings.cache_dir / "vision",
        settings.table_vision_max_pages,
        settings.figure_model if settings.figures else None,
        settings.figure_model if settings.ocr else None,
        settings.figure_model,
        settings.selective_vision,
    )


@dataclass(frozen=True)
class IngestResult:
    pages: int
    ocr_pages: int
    chunks: list[Chunk]
    embed_texts: list[str]  # what each chunk's vector was computed from; also the reranker's input
    vectors: list[list[float]]
    figures: dict[str, bytes]  # crop name → PNG, only the crops a chunk points at
    embedding_model: str


async def ingest(path: Path, title: str, vision_cfg: VisionConfig | None) -> IngestResult:
    """The document's chunks and vectors. Raises IngestError for a PDF that cannot be indexed (its reason is
    the document's), and anything else (a provider outage) for the caller to retry. Call inside
    costs.recording() to get the ingest's cost record."""
    record = costs.current()
    if record:
        record.title = title
    settings = get_settings()
    # a bad or oversized PDF fails with its own reason, before any vision call
    await pdf.in_thread(_check, path, settings.ingest_max_pages)
    pages, ocr_pages, chunks, crops = await _prepared(path, vision_cfg)
    if record:
        record.pages, record.chunks = pages, len(chunks)
    if not chunks:
        raise IngestError("NO_TEXT_LAYER", "No text to index")
    if len(chunks) > settings.ingest_max_chunks:
        raise IngestError(
            "TOO_LARGE", f"{len(chunks)} chunks, more than the {settings.ingest_max_chunks} allowed"
        )
    texts = [embed_text(title, c) for c in chunks]
    tally = openai_client.Tally()
    with costs.stage("embed"):
        try:
            vectors = await reuse.embed(texts, tally)
        finally:
            if record:  # the batches answered before a failure are paid for too
                record.embed_inputs, record.embed_tokens = len(chunks), tally.tokens
    figures = {c.image: crops[c.image] for c in chunks if c.image and c.image in crops}
    chunks = [c if c.image in figures else replace(c, image=None) for c in chunks]
    log.info(
        "ingest",
        extra={
            "pages": pages,
            "ocr_pages": ocr_pages,
            "chunks": len(chunks),
            "tables": sum(1 for c in chunks if c.kind == "table"),
            "figures": sum(1 for c in chunks if c.kind == "figure"),
        },
    )
    return IngestResult(
        pages=pages,
        ocr_pages=ocr_pages,
        chunks=chunks,
        embed_texts=texts,
        vectors=vectors,
        figures=figures,
        embedding_model=settings.embedding_model,
    )


async def _see(path: Path, cfg: VisionConfig | None) -> list[vision.PageVision | None] | None:
    """The vision pass over the PDF, or None when it is off."""
    if cfg is None:
        return None
    selected = await pdf.in_thread(select_pages, path, cfg.max_pages) if cfg.selective else None
    log.info(
        "vision_selection",
        extra={
            "selected_pages": len(selected) if selected is not None else cfg.max_pages,
            "selective": cfg.selective,
        },
    )
    seen = await vision.see_pages(
        path,
        model=cfg.model,
        cache_dir=cfg.cache_dir,
        max_pages=cfg.max_pages,
        figure_model=cfg.figure_model,
        ocr_model=cfg.ocr_model,
        selected_pages=selected,
    )
    if cfg.table_model is None:
        return seen
    return await rescue.rescue_tables(path, seen, model=cfg.table_model, cache_dir=cfg.cache_dir)


def _check(path: Path, max_pages: int) -> None:
    with open_checked(path) as doc:
        if doc.page_count > max_pages:
            raise IngestError("TOO_LARGE", f"{doc.page_count} pages, more than the {max_pages} allowed")


def _parse_and_chunk(path: Path, seen: list[vision.PageVision | None] | None) -> tuple[Document, list[Chunk]]:
    document = read_pages(path, seen)
    paras = outline(document.pages, document.tables, document.figures, document.toc, document.columns)
    settings = get_settings()
    with open_checked(path) as raw:
        # Landscape decks use page boundaries; prose keeps section-aware packing across pages.
        slides = settings.slide_chunks and sum(p.rect.width > p.rect.height for p in raw) > len(raw) / 2
    return document, chunk_paras(
        paras,
        document.ocr_pages,
        max_tokens=settings.chunk_tokens,
        overlap=settings.chunk_overlap,
        page_boundaries=slides,
    )


async def _prepared(path: Path, cfg: VisionConfig | None) -> tuple[int, int, list[Chunk], dict[str, bytes]]:
    settings = get_settings()
    options = [
        "parse-v2",
        settings.chunk_tokens,
        settings.chunk_overlap,
        settings.slide_chunks,
        asdict(cfg) if cfg else None,
    ]

    def fingerprint() -> str:
        h = hashlib.sha256(json.dumps(options, sort_keys=True, default=str).encode())
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                h.update(block)
        return h.hexdigest()

    name = await asyncio.to_thread(fingerprint)
    entry = settings.cache_dir / "prepared-v2" / f"{name}.json"
    async with local_cache.singleflight("prepared:" + name):
        saved = await asyncio.to_thread(local_cache.read, entry)
        if isinstance(saved, dict):
            try:
                chunks = [Chunk(**{**c, "cites": tuple(c["cites"])}) for c in saved["chunks"]]
                crops = {k: base64.b64decode(v, validate=True) for k, v in saved["crops"].items()}
                log.info("prepared_cache", extra={"hit": True})
                increment("learning_prepared_cache_hits_total")
                return int(saved["pages"]), int(saved["ocr_pages"]), chunks, crops
            except (KeyError, TypeError, ValueError):
                pass
        with costs.stage("vision"):
            seen = await _see(path, cfg)
        with costs.stage("parse"):
            document, chunks = await pdf.in_thread(_parse_and_chunk, path, seen)
        crops = {f.image: f.png for page in document.figures for f in page}
        record = costs.current()
        # A partial provider failure must remain retryable, rather than freezing a degraded result in cache.
        if not record or not any(k.failed for k in record.kinds.values()):
            await asyncio.to_thread(
                local_cache.write,
                entry,
                {
                    "pages": len(document.pages),
                    "ocr_pages": len(document.ocr_pages),
                    "chunks": [asdict(c) for c in chunks],
                    "crops": {k: base64.b64encode(v).decode() for k, v in crops.items()},
                },
            )
        log.info("prepared_cache", extra={"hit": False})
        increment("learning_prepared_cache_misses_total")
        return len(document.pages), len(document.ocr_pages), chunks, crops


def embed_text(title: str, chunk: Chunk) -> str:
    return passage_text(title, chunk.section_path, chunk.embed_body or chunk.content)


def passage_text(title: str, section_path: str | None, content: str) -> str:
    """Title + the chunk's deepest heading + content: what gets embedded, and what the
    reranker scores. Only the deepest heading: where font sizes don't follow a document's real structure
    (GAO-style reports), the ancestors in section_path can be wrong, and as model input they'd pull unrelated
    chunks toward queries about those headings."""
    leaf = section_path.rsplit(PATH_SEPARATOR, 1)[-1] if section_path else None
    head = f"{title}\n{leaf}" if leaf else title
    return f"{head}\n\n{content}"
