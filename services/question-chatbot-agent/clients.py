"""Calls to the other services. Identity and Content get the user's own token, so their access rules decide what
the chat may see; ai-core gets this agent's service-account token (role ai-client), never the user's."""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from learning_transport import STUDENT, Claims, IdentityClient, ServiceToken, UpstreamError, read_events

from config import Settings

log = logging.getLogger("question-chatbot-agent.clients")

SEARCH_TOP_K = 8
SEARCH_TIMEOUT = 15.0
GENERATE_TIMEOUT = 30.0
STREAM_TIMEOUT = httpx.Timeout(60, connect=5)
USAGE_TIMEOUT = 5.0


def billed(claims: Claims) -> dict[str, str]:
    """Whose daily AI allowance pays for a call: the signed-in user, at the teacher allowance with that role."""
    return {"user_id": claims.subject, "org_id": claims.org, "role": claims.person_role or STUDENT}


@dataclass(frozen=True)
class Done:
    """The end of a streamed reply."""

    model: str | None
    usage_tokens: int | None


class Services:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings, self.http = settings, http
        self.identity = IdentityClient(settings.identity_url, http)
        self.token = ServiceToken(http, settings.token_url, settings.client_id, settings.client_secret)

    # ---- user-token calls (classroom access is learning_transport.require_relation with self.identity)

    async def search(
        self, authorization: str, classroom_id: str, query: str, correlation: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """The top results, reranked, and whether the rerank fell back (then `score` is an RRF score, not
        comparable to the relevance floor)."""
        body = {"classroom_id": classroom_id, "query": query, "top_k": SEARCH_TOP_K, "rerank": True}
        try:
            resp = await self.http.post(
                f"{self.settings.content_url}/v1/search",
                json=body,
                headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                timeout=SEARCH_TIMEOUT,
            )
        except httpx.HTTPError:
            raise UpstreamError("content", 0) from None
        if resp.status_code != 200:
            raise _error("content", resp)
        data = resp.json()
        return data["results"], bool(data.get("rerank_fallback"))

    # ---- ai-core, as this agent

    async def generate(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        schema: dict[str, Any],
        name: str,
        correlation: str,
        billed_to: dict[str, str],
    ) -> str:
        body = {
            "operation": "generate",
            "messages": messages,
            "max_tokens": max_tokens,
            "response_schema": schema,
            "schema_name": name,
            "billed_to": billed_to,
        }
        resp = await self._ai_core("POST", "/v1/inference", correlation, GENERATE_TIMEOUT, json=body)
        output = resp.json().get("output")
        if not isinstance(output, str):
            raise UpstreamError("ai-core", 502, "AI_OUTPUT_REJECTED")
        return output

    async def stream(
        self, messages: list[dict[str, str]], max_tokens: int, correlation: str, billed_to: dict[str, str]
    ) -> AsyncIterator[str | Done]:
        """The reply's text as it arrives, then Done. A failure before the first word or mid-stream raises."""
        body = {
            "operation": "generate",
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            "billed_to": billed_to,
        }
        headers = await self._headers(correlation)
        try:
            async with self.http.stream(
                "POST",
                f"{self.settings.ai_core_url}/v1/inference",
                json=body,
                headers=headers,
                timeout=STREAM_TIMEOUT,
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise self._refused(resp)
                async for event, data in read_events(resp.aiter_lines()):
                    if event == "delta":
                        yield data["text"]
                    elif event == "done":
                        yield Done(data.get("model_version"), data.get("usage_tokens"))
                        return
                    elif event == "error":
                        raise UpstreamError("ai-core", 502, data.get("code", "AI_PROVIDER_FAILED"))
        except httpx.HTTPError:
            raise UpstreamError("ai-core", 0) from None
        raise UpstreamError("ai-core", 502, "STREAM_ENDED_EARLY")

    async def usage(self, billed_to: dict[str, str], correlation: str) -> dict[str, Any]:
        """The user's AI spend today, their daily limit and when it resets, from ai-core's ledger."""
        resp = await self._ai_core("GET", "/v1/usage", correlation, USAGE_TIMEOUT, params=billed_to)
        data: dict[str, Any] = resp.json()
        return data

    async def _ai_core(
        self, method: str, path: str, correlation: str, timeout: float, **request: Any
    ) -> httpx.Response:
        headers = await self._headers(correlation)
        try:
            resp = await self.http.request(
                method, f"{self.settings.ai_core_url}{path}", headers=headers, timeout=timeout, **request
            )
        except httpx.HTTPError:
            raise UpstreamError("ai-core", 0) from None
        if resp.status_code != 200:
            raise self._refused(resp)
        return resp

    async def _headers(self, correlation: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token.get()}", "X-Correlation-ID": correlation}

    def _refused(self, resp: httpx.Response) -> UpstreamError:
        """ai-core's error; a refused token is dropped so the next call fetches a new one."""
        if resp.status_code == 401:
            self.token.reset()
        return _error("ai-core", resp)


def _error(service: str, resp: httpx.Response) -> UpstreamError:
    """The service's error code and, for AI_SPEND_LIMIT, when the allowance resets."""
    try:
        error = resp.json()["error"]
        code, resets_at = str(error.get("code") or ""), error.get("resets_at")
    except (ValueError, KeyError, TypeError, AttributeError):
        code, resets_at = "", None
    return UpstreamError(service, resp.status_code, code, str(resets_at) if resets_at else None)
