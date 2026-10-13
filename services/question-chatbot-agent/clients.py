"""Calls to the other services. Identity and Content get the user's own token, so their access rules decide what
the chat may see; ai-core gets this agent's service-account token (role ai-client), never the user's."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from config import Settings

log = logging.getLogger("question-chatbot-agent.clients")

SEARCH_TOP_K = 8


class UpstreamError(Exception):
    """Another service answered with an error or couldn't be reached (status 0). `resets_at` comes with
    AI_SPEND_LIMIT: the user's daily AI allowance is used up until then."""

    def __init__(self, service: str, status: int, code: str = "", resets_at: str | None = None) -> None:
        super().__init__(f"{service} {status} {code}".strip())
        self.service, self.status, self.code, self.resets_at = service, status, code, resets_at


def billed(claims: dict[str, Any]) -> dict[str, str]:
    """Whose daily AI allowance pays for a call: the signed-in user, at the teacher allowance with that role."""
    roles = claims.get("realm_access", {}).get("roles", [])
    return {
        "user_id": str(claims["sub"]),
        "org_id": str(claims["org_id"]),
        "role": "teacher" if "teacher" in roles else "student",
    }


@dataclass(frozen=True)
class Done:
    """The end of a streamed reply."""

    model: str | None
    usage_tokens: int | None


class Services:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings, self.http = settings, http
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    # ---- user-token calls

    async def classroom_access(self, authorization: str, classroom_id: str, correlation: str) -> bool:
        try:
            resp = await self.http.get(
                f"{self.settings.identity_url}/v1/classrooms/{classroom_id}/access",
                headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                timeout=5,
            )
        except httpx.HTTPError:
            raise UpstreamError("identity", 0) from None
        if resp.status_code >= 500:
            raise UpstreamError("identity", resp.status_code)
        return resp.status_code == 200

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
                timeout=15,
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
        resp = await self._ai_core(body, correlation, timeout=30)
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
        token = await self._service_token()
        try:
            async with self.http.stream(
                "POST",
                f"{self.settings.ai_core_url}/v1/inference",
                json=body,
                headers=self._headers(token, correlation),
                timeout=httpx.Timeout(60, connect=5),
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    self._refused(resp)
                    raise _error("ai-core", resp)
                event = ""
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
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

    async def _ai_core(self, body: dict[str, Any], correlation: str, timeout: float) -> httpx.Response:
        token = await self._service_token()
        try:
            resp = await self.http.post(
                f"{self.settings.ai_core_url}/v1/inference",
                json=body,
                headers=self._headers(token, correlation),
                timeout=timeout,
            )
        except httpx.HTTPError:
            raise UpstreamError("ai-core", 0) from None
        if resp.status_code != 200:
            self._refused(resp)
            raise _error("ai-core", resp)
        return resp

    async def usage(self, billed_to: dict[str, str], correlation: str) -> dict[str, Any]:
        """The user's AI spend today, their daily limit and when it resets, from ai-core's ledger."""
        token = await self._service_token()
        try:
            resp = await self.http.get(
                f"{self.settings.ai_core_url}/v1/usage",
                params=billed_to,
                headers=self._headers(token, correlation),
                timeout=5,
            )
        except httpx.HTTPError:
            raise UpstreamError("ai-core", 0) from None
        if resp.status_code != 200:
            self._refused(resp)
            raise _error("ai-core", resp)
        data: dict[str, Any] = resp.json()
        return data

    def _headers(self, token: str, correlation: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "X-Correlation-ID": correlation}

    def _refused(self, resp: httpx.Response) -> None:
        if resp.status_code == 401:
            self._token = None  # the next call fetches a new one

    async def _service_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            form = {
                "grant_type": "client_credentials",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
            }
            try:
                resp = await self.http.post(self.settings.token_url, data=form, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError):
                raise UpstreamError("keycloak", 0, "TOKEN_UNAVAILABLE") from None
            self._token = str(data["access_token"])
            self._expires = time.monotonic() + int(data.get("expires_in", 60)) - 30
            return self._token


def _code(resp: httpx.Response) -> str:
    try:
        return str(resp.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return ""


def _error(service: str, resp: httpx.Response) -> UpstreamError:
    resets_at = None
    try:
        value = resp.json()["error"].get("resets_at")
        resets_at = str(value) if value else None
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return UpstreamError(service, resp.status_code, _code(resp), resets_at)
