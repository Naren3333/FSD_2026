"""the vision pass: caching and rendering."""

import asyncio
import json
from pathlib import Path

import pymupdf
import pytest
from conftest import FakeOpenAI, make_pdf, page_key0

from ingest import pages, vision, vision_calls

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
    entry = vision_calls.cache_file(cache, MODEL, page_key0(path))
    entry.parent.mkdir(parents=True)
    entry.write_text(json.dumps({"tables": [], "figures": []}), encoding="utf-8")

    def no_render(*args: object) -> bytes:
        raise AssertionError("rendered a cached page")

    monkeypatch.setattr(vision, "_render", no_render)
    [page] = see(path, cache)
    assert page == vision.PageVision() and openai_fake.requests == []


def test_an_answer_that_is_not_json_is_not_cached(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    cache, path = tmp_path / "cache", pdf(tmp_path)
    openai_fake.reply(json=chat_reply("sorry, I can't"))
    assert see(path, cache) == [None]  # the page falls back to "lines"
    assert not vision_calls.cache_file(cache, MODEL, page_key0(path)).exists()

    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    assert see(path, cache) == [vision.PageVision()]
    assert vision_calls.cache_file(cache, MODEL, page_key0(path)).exists()
    assert len(openai_fake.requests) == 2 and list(cache.rglob("*.tmp")) == []


def test_the_cache_is_keyed_by_what_the_page_shows(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    """the same page in another file (a revised deck) reuses the answer; a changed word doesn't."""
    cache = tmp_path / "cache"
    first, second, changed = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"
    first.write_bytes(make_pdf(paragraphs=[["Some text on the page."]]))
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "A title page that the revised deck adds.")
    doc.insert_pdf(pymupdf.open(first))
    doc.save(second)
    changed.write_bytes(make_pdf(paragraphs=[["Some text on the pages."]]))
    with pymupdf.open(first) as a, pymupdf.open(second) as b, pymupdf.open(changed) as c:
        assert pages.page_key(a[0]) == pages.page_key(b[1]) != pages.page_key(c[0])
    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    see(first, cache)
    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    assert see(second, cache) == [vision.PageVision(), vision.PageVision()]
    assert len(openai_fake.requests) == 2  # only the new title page was sent
    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    see(changed, cache)
    assert len(openai_fake.requests) == 3


def test_odd_fields_in_an_answer_are_coerced_not_fatal() -> None:
    """answers are cached by page content: a field that fails once would fail the same page on every reindex."""
    box = {"bbox": [0.1, 0.1, 0.5, 0.5]}
    table = vision.SeenTable.from_json({**box, "rows": 2, "cols": 2, "caption": 7, "header": 5}, 100, 100)
    assert table is not None and (table.caption, table.header) == ("7", ())
    figure = vision.SeenFigure.from_json({**box, "caption": ["Figure", 1], "title": {"x": 1}}, 100, 100)
    assert figure is not None and (figure.caption, figure.title) == (None, None)
    assert vision.SeenFigure.from_json({**box, "caption": True}, 100, 100).caption is None  # type: ignore[union-attr]
    assert vision.SeenText.from_json({"kind": "table", "header": 3, "rows": [["a"]]}) == vision.SeenText(
        "table", rows=(("a",),)
    )


def test_a_figure_with_an_odd_caption_is_still_seen(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    cache, path = tmp_path / "cache", pdf(tmp_path)
    figure = {"bbox": [0.1, 0.1, 0.5, 0.5], "type": "photo", "informative": True, "caption": [1, 2]}
    for _ in range(2):  # the second run reads the cached answer
        openai_fake.reply(json=chat_reply(json.dumps({"tables": [], "figures": [figure], "blocks": 3})))
        [page] = asyncio.run(
            vision.see_pages(path, model=MODEL, cache_dir=cache, max_pages=500, figure_model="figure-model")
        )
        assert page is not None and [f.caption for f in page.figures] == [None]
    assert len(openai_fake.requests) == 1


RATE_LIMITED = {"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}}


def test_a_rate_limited_call_is_retried_without_nesting(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    """rate_retries attempts in all (not the shared client's retries for each of them), each wait slept outside the vision slots."""
    openai_fake.reply(429, json=RATE_LIMITED, times=vision_calls.RATE_RETRIES)
    assert see(pdf(tmp_path), tmp_path / "cache") == [None]
    assert len(openai_fake.requests) == vision_calls.RATE_RETRIES
    assert openai_fake.sleeps == [2.0 + 3 * n for n in range(vision_calls.RATE_RETRIES - 1)]


def test_retry_after_is_honoured_and_a_long_one_gives_up(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(429, json=RATE_LIMITED, headers={"retry-after": "20"})
    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    assert see(pdf(tmp_path), tmp_path / "cache") == [vision.PageVision()]
    assert openai_fake.sleeps == [20.0]

    openai_fake.reply(429, json=RATE_LIMITED, headers={"retry-after": str(vision_calls.MAX_WAIT + 1)})
    assert see(pdf(tmp_path), tmp_path / "other-cache") == [None]
    assert len(openai_fake.requests) == 3 and openai_fake.sleeps == [20.0]


def test_an_unreadable_cache_entry_is_asked_again(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    cache, path = tmp_path / "cache", pdf(tmp_path)
    entry = vision_calls.cache_file(cache, MODEL, page_key0(path))
    entry.parent.mkdir(parents=True)
    entry.write_text("[1, 2", encoding="utf-8")
    openai_fake.reply(json=chat_reply('{"tables": [], "figures": []}'))
    assert see(path, cache) == [vision.PageVision()]
    assert json.loads(entry.read_text(encoding="utf-8")) == {"tables": [], "figures": []}
