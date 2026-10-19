"""No paid calls: local selection, exact reuse and configurable chunk boundaries."""

from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from conftest import BILLED, FakeOpenAI, MemoryLedger, ingested, make_figure_pdf, make_pdf, textbook
from fastapi.testclient import TestClient

import budget
import spend
from ingest.chunker import chunk_paras
from ingest.headings import Para
from ingest.select import select_pages


def test_clean_prose_and_sparse_title_do_not_need_vision(tmp_path: Path) -> None:
    for text in ("Week 7", "A normal paragraph about photosynthesis. " * 8):
        path = tmp_path / "clean.pdf"
        path.write_bytes(make_pdf(text_=text))
        assert select_pages(path, 500) == set()


def test_a_vector_diagram_keeps_vision_even_with_text(tmp_path: Path) -> None:
    path = tmp_path / "diagram.pdf"
    path.write_bytes(make_figure_pdf(before="A diagram explains the model relationships.")[0])
    assert select_pages(path, 500) == {0}


def test_an_exact_reindex_reuses_preparation_and_embeddings(api: TestClient, embeddings: FakeOpenAI) -> None:
    data = textbook()
    first = ingested(api, data)
    calls = len(embeddings.requests)
    assert calls > 0
    assert ingested(api, data) == first
    assert len(embeddings.requests) == calls


def test_slide_boundaries_and_zero_overlap_are_explicit() -> None:
    paras = [Para("One topic. " * 15, 1, ()), Para("Another topic. " * 15, 2, ())]
    chunks = chunk_paras(paras, max_tokens=200, overlap=0, page_boundaries=True)
    assert [(c.page_start, c.page_end) for c in chunks] == [(1, 1), (2, 2)]
    assert [c.ordinal for c in chunks] == [0, 1]
    with pytest.raises(ValueError):
        chunk_paras(paras, max_tokens=200, overlap=150)


@pytest.mark.anyio
async def test_completed_calls_hold_actual_usage_until_durable_billing(ledger: MemoryLedger) -> None:
    active = budget.Budget(ledger, spend.Billed(**BILLED))
    await active.start()
    token = budget.current.set(active)

    async def provider() -> SimpleNamespace:
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))

    try:
        for _ in range(3):
            await budget.guarded(provider, "gpt-4.1-mini", "Long input " * 1000, 8000)
        actual = spend.cost("gpt-4.1-mini", 10, 2)
        assert ledger.reservations[active.id][1] == pytest.approx(3 * actual)
        await ledger.record_reserved(
            active.id, active.billed, spend.Charge("generate", "gpt-4.1-mini", 30, 6, 3 * actual), "test"
        )
        assert ledger.reservations[active.id][1] == pytest.approx(0)
        await active.close()
        assert active.id not in ledger.reservations
    finally:
        budget.current.reset(token)


@pytest.mark.anyio
async def test_a_timeout_retains_the_reservation_until_accounted_for(ledger: MemoryLedger) -> None:
    active = budget.Budget(ledger, spend.Billed(**BILLED))
    await active.start()
    token = budget.current.set(active)

    async def timeout() -> None:
        raise httpx2.ReadTimeout("provider may still have spent tokens")

    try:
        with pytest.raises(httpx2.ReadTimeout):
            await budget.guarded(timeout, "gpt-4.1-mini", "input", 100)
        await active.close()
        assert ledger.reservations[active.id][1] > 0
    finally:
        budget.current.reset(token)
