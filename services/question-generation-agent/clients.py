"""Calls to the other services. Identity, Curriculum and Content get the teacher's own token, so their access rules
decide what the agent may see; ai-core gets this agent's service-account token (role ai-client), never the
teacher's."""

from dataclasses import dataclass
from typing import Any

import httpx
from learning_transport import Claims, IdentityClient, ServiceToken, UpstreamError, upstream_error

from config import Settings

TOPIC_TIMEOUT = 5.0
SEARCH_TIMEOUT = 15.0
# One structured reply of up to a few thousand tokens.
GENERATE_TIMEOUT = 90.0


def billed(claims: Claims) -> dict[str, str]:
    """Whose daily AI allowance pays: the teacher who asked (ADR 0003)."""
    return {"user_id": claims.subject, "org_id": claims.org, "role": "teacher"}


@dataclass(frozen=True)
class Topic:
    id: str
    name: str


class Services:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings, self.http = settings, http
        self.identity = IdentityClient(settings.identity_url, http)
        self.token = ServiceToken(http, settings.token_url, settings.client_id, settings.client_secret)

    # ---- teacher-token calls (classroom access is learning_transport.require_relation with self.identity)

    async def topic(self, authorization: str, topic_id: str, correlation: str) -> Topic | None:
        """The curriculum topic, or None when Curriculum has no such topic for the teacher."""
        try:
            resp = await self.http.get(
                f"{self.settings.curriculum_url}/v1/topics/{topic_id}",
                headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                timeout=TOPIC_TIMEOUT,
            )
        except httpx.HTTPError:
            raise UpstreamError("curriculum", 0) from None
        if resp.status_code >= 500:
            raise UpstreamError("curriculum", resp.status_code)
        if resp.status_code != 200:
            return None
        try:
            name = resp.json()["name"]
        except (ValueError, KeyError, TypeError):
            raise UpstreamError("curriculum", 502) from None
        if not isinstance(name, str) or not name:
            raise UpstreamError("curriculum", 502)
        return Topic(topic_id, name)

    async def search(
        self, authorization: str, classroom_id: str, query: str, document_ids: list[str], top_k: int,
        correlation: str,
    ) -> tuple[list[dict[str, Any]], bool]:  # fmt: skip
        """The chosen documents' passages that best match the query, reranked, and whether the rerank fell back (then
        `score` is an RRF score, not comparable to the relevance floor). Only teaching materials are searched: a
        rubric or marking scheme uploaded before they were retired (ADR 0009) is never drafted from."""
        body = {
            "classroom_id": classroom_id,
            "query": query,
            "document_ids": document_ids,
            "doc_types": ["teaching_material"],
            "top_k": top_k,
            "rerank": True,
        }
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
            raise upstream_error("content", resp)
        try:
            data = resp.json()
            results, fallback = data["results"], bool(data.get("rerank_fallback"))
        except (ValueError, KeyError, TypeError):
            raise UpstreamError("content", 502) from None
        if not isinstance(results, list) or not all(_hit(r) for r in results):
            raise UpstreamError("content", 502)
        return results, fallback

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
        headers = {"Authorization": f"Bearer {await self.token.get()}", "X-Correlation-ID": correlation}
        try:
            resp = await self.http.post(
                f"{self.settings.ai_core_url}/v1/inference",
                json=body,
                headers=headers,
                timeout=GENERATE_TIMEOUT,
            )
        except httpx.HTTPError:
            raise UpstreamError("ai-core", 0) from None
        if resp.status_code != 200:
            if resp.status_code == 401:  # a refused token is dropped, so the next call fetches a new one
                self.token.reset()
            raise upstream_error("ai-core", resp)
        output = resp.json().get("output")
        if not isinstance(output, str):
            raise UpstreamError("ai-core", 502, "AI_OUTPUT_REJECTED")
        return output


def _hit(result: object) -> bool:
    """Whether a search result has the fields a passage is made from."""
    return (
        isinstance(result, dict)
        and all(isinstance(result.get(k), str) for k in ("chunk_id", "document_id", "content"))
        and isinstance(result.get("score", 0), int | float)
    )
