"""offline pdf extraction/chunk comparison."""

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "python-platform"))
from ingest.chunker import chunk_paras
from ingest.headings import outline
from ingest.parse import read_pages
from ingest.select import select_pages


@dataclass
class Comparison:
    file: str
    pages: int
    local_parse_seconds: float
    vision_candidates: list[int]
    variants: list[dict[str, int | float | bool]]


def compare(path: Path) -> Comparison:
    started = time.perf_counter()
    document = read_pages(path)
    paras = outline(document.pages, document.tables, document.figures, document.toc, document.columns)
    elapsed = time.perf_counter() - started
    with pymupdf.open(path) as doc:
        slides = sum(p.rect.width > p.rect.height for p in doc) > len(doc) / 2
    variants = []
    for size in (400, 600, 800):
        for overlap in (0, size * 15 // 100):
            start = time.perf_counter()
            chunks = chunk_paras(paras, max_tokens=size, overlap=overlap, page_boundaries=slides)
            variants.append(
                {
                    "chunk_limit": size,
                    "overlap": overlap,
                    "slide_boundaries": slides,
                    "chunks": len(chunks),
                    "tokens": sum(c.token_count for c in chunks),
                    "chunk_seconds": round(time.perf_counter() - start, 4),
                }
            )
    return Comparison(
        path.name,
        len(document.pages),
        round(elapsed, 4),
        [n + 1 for n in sorted(select_pages(path, 500))],
        variants,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [asdict(compare(path)) for path in args.files]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "mode": "offline; no embeddings, model calls or retrieval quality measurement",
                "documents": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Compared {len(rows)} PDFs; wrote {args.output}")


if __name__ == "__main__":
    main()
