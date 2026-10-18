"""Calls to the other services. Identity, Curriculum and Content get the teacher's own token, so their access rules
decide what the agent may see; ai-core gets this agent's service-account token (role ai-client), never the
teacher's. The transport is learning_transport.agents."""

from dataclasses import dataclass
from typing import Any

import httpx
from learning_transport import Claims, UpstreamError
from learning_transport.agents import AgentClients, billed_to

from config import Settings

TOPIC_TIMEOUT = 5.0
SEARCH_TIMEOUT = 15.0
# One structured reply of up to a few thousand tokens.
GENERATE_TIMEOUT = 90.0


def billed(claims: Claims) -> dict[str, str]:
    """Whose daily AI allowance pays: the teacher who asked (ADR 0003)."""
    return billed_to(claims, "teacher")


@dataclass(frozen=True)
class Topic:
    id: str
    name: str


class Services(AgentClients):
    settings: Settings  # with Curriculum's URL
    generate_timeout = GENERATE_TIMEOUT

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        super().__init__(settings, http)

    # ---- teacher-token calls

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
        return await self.search_content(authorization, body, correlation, SEARCH_TIMEOUT)
