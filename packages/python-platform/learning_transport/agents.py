"""the calls an ai agent makes to identity, content and ai-core, and the settings they need."""

import asyncio
import os
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from fastapi import HTTPException

from learning_transport.app import require_env
from learning_transport.auth import Claims
from learning_transport.errors import UpstreamError, failure, upstream_error
from learning_transport.identity import IdentityClient
from learning_transport.tokens import ServiceToken

# the settings every agent reads, by field and environment variable.
AGENT_ENV = {
    "identity_url": "IDENTITY_URL",
    "content_url": "CONTENT_URL",
    "ai_core_url": "AI_CORE_URL",
    "token_url": "AI_TOKEN_URL",
    "client_id": "AI_CLIENT_ID",
    "client_secret": "AI_CLIENT_SECRET",
}


@dataclass(frozen=True)
class AgentSettings:
    identity_url: str
    content_url: str
    ai_core_url: str
    token_url: str  # keycloak's token endpoint, for the agent's own service account (ai-core calls only)
    client_id: str
    client_secret: str


def read_settings(required: dict[str, str]) -> dict[str, str]:
    """each field's environment variable, without a trailing slash."""
    require_env(*required.values())
    return {field: os.environ[name].rstrip("/") for field, name in required.items()}


def billed_to(claims: Claims, role: str) -> dict[str, str]:
    """ai-core's `billed_to`: the signed-in user, at the daily allowance of `role`."""
    return {"user_id": claims.subject, "org_id": claims.org, "role": role}


# ai-core errors whose code alone says what the user should hear.
AI_CORE_CODES = {
    "AI_BUSY": (429, "You have too many AI requests running at once. Try again in a moment."),
    "AI_PROVIDER_NOT_CONFIGURED": (503, "AI is not set up on this server."),
    "AI_OUTPUT_REJECTED": (502, "The model did not return a usable reply. Try again."),
    "AI_TIMEOUT": (504, "The model took too long to answer. Try again."),
}
# a model call the caller's deadline stopped or kept from starting.
OUT_OF_TIME = "OUT_OF_TIME"
# a call with less time than this left before its deadline is not started.
MIN_CALL = 5.0
# ai-core gives up this long before the agent does, leaving time for its allowance checks and accounting.
REPLY_MARGIN = 5.0


def ai_core_refusal(e: UpstreamError) -> HTTPException | None:
    """ai-core's own refusals (spend limit, busy, not configured, unusable output) with a safe message; None for anything else."""
    if e.code == "AI_SPEND_LIMIT":
        when = _local_time(e.resets_at)
        message = "You've used today's AI allowance." + (f" It resets at {when}." if when else "")
        return failure(429, "AI_SPEND_LIMIT", message)
    if e.code in AI_CORE_CODES:
        status, message = AI_CORE_CODES[e.code]
        return failure(status, e.code, message)
    return None


def unusable_reply() -> HTTPException:
    """the model's reply could not be read or used."""
    return failure(502, "AI_OUTPUT_REJECTED", AI_CORE_CODES["AI_OUTPUT_REJECTED"][1])


def strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    """an object whose every property is required and nothing else allowed, as strict structured output needs."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


async def gather_all(*calls: Awaitable[Any]) -> list[Any]:
    """every call's result, in order; the first failure cancels the others and is raised as itself."""
    tasks: list[asyncio.Task[Any]] = []
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(_awaited(c)) for c in calls]
    except BaseExceptionGroup as failed:
        raise failed.exceptions[0] from None
    return [t.result() for t in tasks]


async def _awaited(call: Awaitable[Any]) -> Any:
    return await call


def _local_time(iso: str | None) -> str:
    """ai-core's reset time in its own zone (SPEND_TIMEZONE), with the zone's offset, e.g. \"00:00 on 19 oct (utc+08:00)\": the user's browser may be elsewhere."""
    try:
        at = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return ""
    if at is None:
        return ""
    zone = at.strftime(" (%Z)") if at.tzinfo is not None else ""
    return at.strftime("%H:%M on %d %b") + zone


class AgentClients:
    """share authenticated identity, search and inference clients with bounded generation timeouts."""

    generate_timeout = 30.0

    def __init__(self, settings: AgentSettings, http: httpx.AsyncClient) -> None:
        self.settings, self.http = settings, http
        self.identity = IdentityClient(settings.identity_url, http)
        self.token = ServiceToken(http, settings.token_url, settings.client_id, settings.client_secret)

    # ---- user-token calls

    async def search_content(
        self, authorization: str, body: dict[str, Any], correlation: str, timeout: float
    ) -> tuple[list[dict[str, Any]], bool]:
        """content's search results for `body`, and whether its rerank fell back (then `score` is an rrf score, not comparable to a relevance floor)."""
        try:
            resp = await self.http.post(
                f"{self.settings.content_url}/v1/search",
                json=body,
                headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                timeout=timeout,
            )
        except httpx.HTTPError:
            raise UpstreamError("content", 0) from None
        if resp.status_code != 200:
            raise upstream_error("content", resp)
        try:
            data = resp.json()
            results, fallback = data["results"], bool(data.get("rerank_fallback"))
        except (ValueError, KeyError, TypeError):
            raise UpstreamError("content", 502) from None
        if not isinstance(results, list) or not all(_hit(r) for r in results):
            raise UpstreamError("content", 502)
        return results, fallback

    # ---- ai-core, as the agent

    async def generate(  # noqa: PLR0913 - one call's schema, payer and optional cut-off
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        schema: dict[str, Any],
        name: str,
        correlation: str,
        payer: dict[str, str],
        deadline: float | None = None,
    ) -> str:
        """one structured reply: the model's output as text, matching `schema`, billed to `payer` (billed_to()). with a `deadline` (time.monotonic()) the call is cut off then, and one it stopped or kept from starting raises OUT_OF_TIME."""
        limit = self.generate_timeout
        if deadline is not None:
            left = deadline - time.monotonic()
            if left < MIN_CALL:
                raise UpstreamError("ai-core", 0, OUT_OF_TIME)
            limit = min(left, limit)
        body = {
            "operation": "generate",
            "messages": messages,
            "max_tokens": max_tokens,
            "response_schema": schema,
            "schema_name": name,
            "billed_to": payer,
            # ai-core stops first, so a reply is never finished and billed after the agent has stopped waiting.
            "timeout": max(limit - REPLY_MARGIN, 1.0),
        }
        try:
            resp = await self.ai_core("POST", "/v1/inference", correlation, limit, json=body)
        except UpstreamError as e:
            timed_out = e.status == 0 or e.code == "AI_TIMEOUT"
            if timed_out and limit < self.generate_timeout:
                raise UpstreamError("ai-core", 0, OUT_OF_TIME) from None
            raise
        try:
            output = resp.json().get("output")
        except (ValueError, AttributeError):
            output = None
        if not isinstance(output, str):
            raise UpstreamError("ai-core", 502, "AI_OUTPUT_REJECTED")
        return output

    async def ai_core(
        self, method: str, path: str, correlation: str, timeout: float, **request: Any
    ) -> httpx.Response:
        """a 200 response from ai-core, or its error as UpstreamError."""
        headers = await self.ai_core_headers(correlation)
        try:
            resp = await self.http.request(
                method, f"{self.settings.ai_core_url}{path}", headers=headers, timeout=timeout, **request
            )
        except httpx.HTTPError:
            raise UpstreamError("ai-core", 0) from None
        if resp.status_code != 200:
            raise self.ai_core_refused(resp)
        return resp

    async def ai_core_headers(self, correlation: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token.get()}", "X-Correlation-ID": correlation}

    def ai_core_refused(self, resp: httpx.Response) -> UpstreamError:
        """ai-core's error; a refused token is dropped so the next call fetches a new one."""
        if resp.status_code == 401:
            self.token.reset()
        return upstream_error("ai-core", resp)


def _hit(result: object) -> bool:
    """whether a search result has the fields a passage is made from."""
    return (
        isinstance(result, dict)
        and all(isinstance(result.get(k), str) for k in ("chunk_id", "document_id", "content"))
        and isinstance(result.get("score", 0), int | float)
    )
