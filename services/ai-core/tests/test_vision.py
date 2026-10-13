"""The vision pass (RAG_PLAN Iterations 10-11): caching and rendering. OpenAI is the mock transport."""

import asyncio
import json
from pathlib import Path

import pymupdf
import pytest
from conftest import FakeOpenAI, make_pdf, page_key0

from ingest import vision

MODEL = "vision-model"


def chat_reply(content: str) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(make_pdf(paragraphs=[["Some text on the page."]]))
    return path


def see(path: Path, cache: Path) -> list:
    return asyncio.run(vision.see_pages(path, model=MODEL, cache_dir=cache, max_pages=500))


def test_a_cached_page_is_neither_rendered_nor_sent(
    tmp_path: Path, openai_fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, path = tmp_path / "cache", pdf(tmp_path)
    entry = vision.cache_file(cache, MODEL, page_key0(path))
    entry.parent.mkdir(parents=True)
    entry.write_text(json.dumps({"tables": [], "figures": []}), encoding="utf-8")

    def no_render(*args):
        raise AssertionError("rendered a cached page")

    monkeypatch.setattr(vision, "_render", no_render)
    [page] = see(path, cache)
    assert page == vision.PageVision() and openai_fake.requests == []


def test_an_answer_that_is_not_json_is_not_cached(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    cache, path = tmp_path / "cache", pdf(tmp_path)
    openai_fake.reply(json=chat_reply("sorry, I can't"))
    assert see(path, cache) == [None]  # the page falls back to "lines"
    assert not vision.cache_file(cache, MODEL, page_key0(path)).exists()

    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    assert see(path, cache) == [vision.PageVision()]
    assert vision.cache_file(cache, MODEL, page_key0(path)).exists()
    assert len(openai_fake.requests) == 2 and list(cache.rglob("*.tmp")) == []


def test_the_cache_is_keyed_by_what_the_page_shows(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    """RAG_PLAN Iteration 14: the same page in another file (a revised deck) reuses the answer; a changed word
    doesn't."""
    cache = tmp_path / "cache"
    first, second, changed = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"
    first.write_bytes(make_pdf(paragraphs=[["Some text on the page."]]))
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "A title page that the revised deck adds.")
    doc.insert_pdf(pymupdf.open(first))
    doc.save(second)
    changed.write_bytes(make_pdf(paragraphs=[["Some text on the pages."]]))
    with pymupdf.open(first) as a, pymupdf.open(second) as b, pymupdf.open(changed) as c:
        assert vision.page_key(a[0]) == vision.page_key(b[1]) != vision.page_key(c[0])
    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    see(first, cache)
    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    assert see(second, cache) == [vision.PageVision(), vision.PageVision()]
    assert len(openai_fake.requests) == 2  # only the new title page was sent
    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    see(changed, cache)
    assert len(openai_fake.requests) == 3
