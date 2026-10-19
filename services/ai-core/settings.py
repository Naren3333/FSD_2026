"""ai-core settings, read once from the environment. Provider keys exist only here (ADR 0002)."""

import json
import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# The daily AI allowance defaults (ADR 0003), in USD per user per day, and the zone whose midnight starts the day.
SPEND_LIMIT_STUDENT_USD, SPEND_LIMIT_TEACHER_USD, SPEND_TIMEZONE = 5.0, 10.0, "Asia/Singapore"
USER_CONCURRENCY = 10
# The largest PDF ingest takes, in pages and in chunks: past either it fails TOO_LARGE. Pages are checked before any
# model call; chunks before the embeddings.
INGEST_MAX_PAGES, INGEST_MAX_CHUNKS = 500, 5000


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


def _secret(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    embedding_model: str
    chat_model: str
    chat_reasoning_effort: str
    voyage_api_key: str | None
    voyage_base_url: str
    reranker_model: str
    # The vision pass (tables, figures, OCR). Off, or past the page cap: tables come from find_tables "lines" only.
    # Not CHAT_MODEL: OpenAI rate limits are per model, so a big ingest doesn't starve chat.
    table_vision: bool
    table_vision_model: str
    table_vision_max_pages: int
    figures: bool
    figure_model: str
    ocr: bool
    # Model answers keyed by page content, so a reindex is free. Operational cache only, never business data.
    cache_dir: Path
    # The daily AI allowance (ADR 0003): per role, with the day starting at midnight in spend_timezone.
    spend_limit_student_usd: float = SPEND_LIMIT_STUDENT_USD
    spend_limit_teacher_usd: float = SPEND_LIMIT_TEACHER_USD
    spend_timezone: str = SPEND_TIMEZONE
    # Running requests per user, shared across replicas through PostgreSQL reservations (including ingest).
    user_concurrency: int = USER_CONCURRENCY
    ingest_max_pages: int = INGEST_MAX_PAGES
    ingest_max_chunks: int = INGEST_MAX_CHUNKS
    # USD per million tokens (input, cached input, output) that override or add to spend.DEFAULT_PRICES.
    model_prices: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    selective_vision: bool = True
    chunk_tokens: int = 600
    chunk_overlap: int = 90
    slide_chunks: bool = True
    # Optional operational ceiling across every user and all usage retained in this ledger; never a daily reset.
    total_spend_limit_usd: float | None = None

    def __post_init__(self) -> None:
        if self.total_spend_limit_usd is not None and (
            not math.isfinite(self.total_spend_limit_usd) or self.total_spend_limit_usd <= 0
        ):
            raise ValueError("AI_TOTAL_SPEND_LIMIT_USD must be finite and positive")
        if not 200 <= self.chunk_tokens <= 2000 or not 0 <= self.chunk_overlap < self.chunk_tokens // 2:
            raise ValueError("CHUNK_TOKENS must be 200–2000; CHUNK_OVERLAP must be less than half of it")
        if not 1 <= self.user_concurrency <= 100:
            raise ValueError("AI_USER_CONCURRENCY must be 1–100")
        if not 1 <= self.ingest_max_pages <= 500 or not 1 <= self.ingest_max_chunks <= 5000:
            raise ValueError("Ingest limits must be 1–500 pages and 1–5000 chunks")


def _prices(raw: str) -> dict[str, tuple[float, float, float]]:
    """MODEL_PRICES: a JSON object of model name to [input, cached input, output] USD per million tokens."""
    if not raw.strip():
        return {}
    return {model: (float(a), float(b), float(c)) for model, (a, b, c) in json.loads(raw).items()}


@lru_cache
def get_settings() -> Settings:
    env = os.environ.get
    return Settings(
        openai_api_key=_secret("OPENAI_API_KEY"),
        embedding_model=env("EMBEDDING_MODEL") or "text-embedding-3-small",
        chat_model=env("CHAT_MODEL") or "gpt-6-luna",
        chat_reasoning_effort=env("CHAT_REASONING_EFFORT") or "none",
        voyage_api_key=_secret("VOYAGE_API_KEY"),
        voyage_base_url=env("VOYAGE_BASE_URL") or "https://api.voyageai.com/v1",
        reranker_model=env("RERANKER_MODEL") or "rerank-3",
        table_vision=_flag("TABLE_VISION", True),
        table_vision_model=env("TABLE_VISION_MODEL") or "gpt-5.4-mini",
        table_vision_max_pages=int(env("TABLE_VISION_MAX_PAGES") or 500),
        figures=_flag("FIGURES", True),
        figure_model=env("FIGURE_MODEL") or "gpt-6-luna",
        ocr=_flag("OCR", True),
        cache_dir=Path(env("AI_CORE_CACHE_DIR") or "/var/cache/ai-core"),
        spend_limit_student_usd=float(env("SPEND_LIMIT_STUDENT_USD") or SPEND_LIMIT_STUDENT_USD),
        spend_limit_teacher_usd=float(env("SPEND_LIMIT_TEACHER_USD") or SPEND_LIMIT_TEACHER_USD),
        spend_timezone=env("SPEND_TIMEZONE") or SPEND_TIMEZONE,
        user_concurrency=int(env("AI_USER_CONCURRENCY") or USER_CONCURRENCY),
        ingest_max_pages=int(env("INGEST_MAX_PAGES") or INGEST_MAX_PAGES),
        ingest_max_chunks=int(env("INGEST_MAX_CHUNKS") or INGEST_MAX_CHUNKS),
        model_prices=_prices(env("MODEL_PRICES") or ""),
        selective_vision=_flag("SELECTIVE_VISION", True),
        chunk_tokens=int(env("CHUNK_TOKENS") or 600),
        chunk_overlap=int(env("CHUNK_OVERLAP") or 90),
        slide_chunks=_flag("SLIDE_CHUNKS", True),
        total_spend_limit_usd=float(total) if (total := env("AI_TOTAL_SPEND_LIMIT_USD")) else None,
    )
