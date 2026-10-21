"""Structured ingestion for PDFs and native Markdown/PPTX/DOCX teaching materials.

Content owns business rows, originals, vectors and crops. ai-core keeps operational caches
for prepared structure, vision results and exact embedding inputs. Native readers are local;
PDFs retain their existing selective-vision path. All uncached model work uses ai-core billing.

Source-unit and chunk limits fail before embeddings. Native source anchors are slides or
logical sections, not inferred Word/Markdown pages; see ADR 0013.
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
from ingest import costs, native, rescue, reuse, vision
from ingest.chunker import PATH_SEPARATOR, Chunk, chunk_paras, encoding
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


async def ingest(
    path: Path, title: str, vision_cfg: VisionConfig | None, mime: str = "application/pdf"
) -> IngestResult:
    """The document's chunks and vectors. Raises IngestError for a PDF that cannot be indexed (its reason is
    the document's), and anything else (a provider outage) for the caller to retry. Call inside
    costs.recording() to get the ingest's cost record."""
    record = costs.current()
    if record:
        record.title = title
    settings = get_settings()
    # a bad or oversized PDF fails with its own reason, before any vision call
    if mime == "application/pdf":
        await pdf.in_thread(_check, path, settings.ingest_max_pages)
        pages, ocr_pages, chunks, crops = await _prepared(path, vision_cfg)
    else:
        pages, ocr_pages, chunks, crops = await _native_prepared(path, mime)
        if pages > settings.ingest_max_pages:
            raise IngestError("TOO_LARGE", "Too many slides or logical sections")
    if record:
        record.pages, record.chunks = pages, len(chunks)
    if not chunks:
        raise IngestError("NO_TEXT_LAYER", "No text to index")
    if len(chunks) > settings.ingest_max_chunks:
        raise IngestError(
            "TOO_LARGE", f"{len(chunks)} chunks, more than the {settings.ingest_max_chunks} allowed"
        )
    chunks = await asyncio.to_thread(_storage_chunks, chunks)
    texts = [embed_text(title, c) for c in chunks]
    if mime != "application/pdf":

        def oversized_native_input() -> bool:
            return any(
                len(c.content) > 20000 or len(encoding().encode(text, disallowed_special=())) > 2200
                for c, text in zip(chunks, texts, strict=True)
            )

        if await asyncio.to_thread(oversized_native_input):
            raise IngestError("TOO_LARGE", "A native section path or table exceeds the embedding-input limit")
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


async def _native_prepared(path: Path, mime: str) -> tuple[int, int, list[Chunk], dict[str, bytes]]:
    settings = get_settings()
    if mime not in native.FORMATS:
        raise IngestError("CORRUPT", "Unsupported native document type")
    options = f"native-v1|{mime}|{settings.chunk_tokens}|{settings.chunk_overlap}|"

    def fingerprint() -> str:
        hashed = hashlib.sha256(options.encode())
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                hashed.update(block)
        return hashed.hexdigest()

    name = await asyncio.to_thread(fingerprint)
    entry = settings.cache_dir / "prepared-native-v1" / f"{name}.json"
    async with local_cache.singleflight("native:" + name):
        saved = await asyncio.to_thread(local_cache.read, entry)
        if isinstance(saved, dict):
            try:
                chunks = [Chunk(**{**c, "cites": tuple(c["cites"])}) for c in saved["chunks"]]
                increment("learning_prepared_cache_hits_total")
                return int(saved["units"]), 0, chunks, {}
            except (KeyError, TypeError, ValueError):
                pass

        def prepare() -> tuple[int, list[Chunk]]:
            document = native.read(path, mime, settings.ingest_max_pages)
            if document.unit > settings.ingest_max_pages:
                raise IngestError("TOO_LARGE", "Too many slides or logical sections")
            chunks = chunk_paras(
                document.paras,
                max_tokens=settings.chunk_tokens,
                overlap=settings.chunk_overlap,
                page_boundaries=True,
            )
            return document.unit, [replace(c, source=native.FORMATS[mime]) for c in chunks]

        with costs.stage("parse"):
            units, chunks = await pdf.in_thread(prepare)
        if len(chunks) > settings.ingest_max_chunks:
            raise IngestError("TOO_LARGE", "Too many document chunks")
        await asyncio.to_thread(
            local_cache.write, entry, {"units": units, "chunks": [asdict(c) for c in chunks]}
        )
        increment("learning_prepared_cache_misses_total")
        return units, 0, chunks, {}


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
        slides = (
            settings.slide_chunks
            and sum(raw.load_page(n).rect.width > raw.load_page(n).rect.height for n in range(raw.page_count))
            > raw.page_count / 2
        )
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


def _storage_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """Normalize missing PDF glyphs before embedding and storage, including older prepared-cache entries.

    PostgreSQL text cannot contain NUL. Keep an explicit replacement glyph instead of silently joining words.
    Exact embedding keys change only for affected chunks, so retrying need not pay for vision again.
    """

    def optional(text: str | None) -> str | None:
        return text.replace("\x00", "\ufffd") if text is not None else None

    cleaned = []
    for chunk in chunks:
        values = (chunk.content, chunk.embed_body, chunk.section_path, chunk.label, *chunk.cites)
        if not any(text is not None and "\x00" in text for text in values):
            cleaned.append(chunk)
            continue
        content = chunk.content.replace("\x00", "\ufffd")
        cleaned.append(
            replace(
                chunk,
                content=content,
                embed_body=optional(chunk.embed_body),
                section_path=optional(chunk.section_path),
                label=optional(chunk.label),
                cites=tuple(s.replace("\x00", "\ufffd") for s in chunk.cites),
                token_count=len(encoding().encode(content, disallowed_special=())),
            )
        )
    return cleaned


def embed_text(title: str, chunk: Chunk) -> str:
    return passage_text(title, chunk.section_path, chunk.embed_body or chunk.content, chunk.source)


def passage_text(title: str, section_path: str | None, content: str, source: str = "pdf") -> str:
    """Native structure retains the full heading path for both embedding and reranking.
    PDFs retain the existing deepest-heading rule: where font sizes don't follow a document's real structure
    (GAO-style reports), the ancestors in section_path can be wrong, and as model input they'd pull unrelated
    chunks toward queries about those headings."""
    leaf = (
        (section_path if source in native.FORMATS.values() else section_path.rsplit(PATH_SEPARATOR, 1)[-1])
        if section_path
        else None
    )
    head = f"{title}\n{leaf}" if leaf else title
    return f"{head}\n\n{content}"
