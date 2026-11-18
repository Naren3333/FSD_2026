"""overlapping embedding batches share cached work without paid providers."""

import asyncio
from types import SimpleNamespace

import pytest

import openai_client
from ingest import reuse


@pytest.mark.anyio
async def test_overlapping_batches_embed_each_input_once(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reuse, "get_settings", lambda: SimpleNamespace(cache_dir=tmp_path, embedding_model="test")
    )
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def embed(texts, tally):
        calls.extend(texts)
        started.set()
        await release.wait()
        tally.tokens += len(texts)
        return [[float(ord(text[0]))] * 1536 for text in texts], len(texts)

    monkeypatch.setattr(openai_client, "embed", embed)
    first_tally, second_tally = openai_client.Tally(), openai_client.Tally()
    async with asyncio.timeout(5):
        first = asyncio.create_task(reuse.embed(["a", "b"], first_tally))
        await started.wait()
        second = asyncio.create_task(reuse.embed(["c", "b", "c"], second_tally))
        release.set()
        a, b = await asyncio.gather(first, second)
    assert sorted(calls) == ["a", "b", "c"]
    assert a[1] == b[1] and b[0] == b[2]
    assert first_tally.tokens + second_tally.tokens == 3


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [True, False])
async def test_failed_owner_releases_input_locks(tmp_path, monkeypatch, cancel):
    monkeypatch.setattr(
        reuse, "get_settings", lambda: SimpleNamespace(cache_dir=tmp_path, embedding_model="test")
    )
    started = asyncio.Event()

    async def failing(texts, tally):
        started.set()
        if cancel:
            await asyncio.Event().wait()
        raise ValueError("provider failed")

    monkeypatch.setattr(openai_client, "embed", failing)
    owner = asyncio.create_task(reuse.embed(["a", "b"], openai_client.Tally()))
    await started.wait()
    if cancel:
        owner.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ValueError):
        await owner

    async def succeeding(texts, tally):
        return [[1.0] * 1536 for _ in texts], len(texts)

    monkeypatch.setattr(openai_client, "embed", succeeding)
    async with asyncio.timeout(5):
        assert len(await reuse.embed(["b", "a"], openai_client.Tally())) == 2
