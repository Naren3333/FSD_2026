"""ai-core settings, read once from the environment. Provider keys exist only here (ADR 0002)."""

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


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
    spend_limit_student_usd: float = 5.0
    spend_limit_teacher_usd: float = 10.0
    spend_timezone: str = "Asia/Singapore"
    # USD per million tokens (input, cached input, output) that override or add to spend.DEFAULT_PRICES.
    model_prices: dict[str, tuple[float, float, float]] = field(default_factory=dict)


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
        spend_limit_student_usd=float(env("SPEND_LIMIT_STUDENT_USD") or 5),
        spend_limit_teacher_usd=float(env("SPEND_LIMIT_TEACHER_USD") or 10),
        spend_timezone=env("SPEND_TIMEZONE") or "Asia/Singapore",
        model_prices=_prices(env("MODEL_PRICES") or ""),
    )
