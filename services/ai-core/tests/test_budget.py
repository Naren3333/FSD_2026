"""budget: the tokens a provider reply reports, which settle a reservation."""

from types import SimpleNamespace

import httpx2

from budget import _tokens_used


def test_an_openai_reply_reports_input_output_and_cached_tokens() -> None:
    usage = SimpleNamespace(
        prompt_tokens=120, completion_tokens=30, prompt_tokens_details=SimpleNamespace(cached_tokens=100)
    )
    assert _tokens_used(SimpleNamespace(usage=usage)) == (120, 30, 100)


def test_missing_usage_counts_nothing_known() -> None:
    assert _tokens_used(SimpleNamespace()) == (None, 0, 0)
    no_details = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5, completion_tokens=None))
    assert _tokens_used(no_details) == (5, 0, 0)


def test_an_http_reply_reports_its_total_tokens() -> None:
    assert _tokens_used(httpx2.Response(200, json={"usage": {"total_tokens": 42}})) == (42, 0, 0)


def test_an_unreadable_or_failed_http_reply_reports_no_inputs() -> None:
    assert _tokens_used(httpx2.Response(200, content=b"not json")) == (None, 0, 0)
    assert _tokens_used(httpx2.Response(200, json=["no", "usage"])) == (None, 0, 0)
    assert _tokens_used(httpx2.Response(200, json={"usage": {"total_tokens": "many"}})) == (None, 0, 0)
    assert _tokens_used(httpx2.Response(500, json={"usage": {"total_tokens": 42}})) == (None, 0, 0)
