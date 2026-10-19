"""Offline prompt token counting with the same committed tokenizer used by ai-core."""

import os
from functools import cache
from pathlib import Path

import tiktoken


@cache
def encoding() -> tiktoken.Encoding:
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path(__file__).parent / "tiktoken"))
    return tiktoken.get_encoding("cl100k_base")


def count(text: str) -> int:
    return len(encoding().encode(text, disallowed_special=()))
