"""share authenticated retrieval and inference clients."""

from dataclasses import dataclass

import httpx
from learning_transport import TEACHER, Claims, UpstreamError, upstream_error
from learning_transport.agents import AgentClients, billed_to

from config import Settings
from upstream import SearchPassage, read_passages, read_titles

TOPIC_TIMEOUT = 5.0
DOCUMENTS_TIMEOUT = 5.0
DOCUMENTS_PAGE = 100  # content's largest page
# pages overlap, so documents deleted between two page reads do not move a chosen one past both.
DOCUMENTS_OVERLAP = 10
SEARCH_TIMEOUT = 15.0
# allow enough time for bounded generation, allowance checks and usage accounting.
GENERATE_TIMEOUT = 135.0


def billed(claims: Claims) -> dict[str, str]:
    """whose daily ai allowance pays: the teacher who asked (adr 0003)."""
    return billed_to(claims, TEACHER)


@dataclass(frozen=True)
class Topic:
    id: str
    name: str


class Services(AgentClients):
    settings: Settings  # with curriculum's url
    generate_timeout = GENERATE_TIMEOUT

    # ---- caller-token calls

    async def topic(self, authorization: str, topic_id: str, correlation: str) -> Topic | None:
        """the curriculum topic, or none when curriculum has no such topic for the caller."""
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
    ) -> tuple[list[SearchPassage], bool]:  # fmt: skip
        """the chosen documents' passages that best match the query, reranked, and whether the rerank fell back (then `score` is an rrf score, not comparable to the relevance floor)."""
        body = {
            "classroom_id": classroom_id,
            "query": query,
            "document_ids": document_ids,
            "doc_types": ["teaching_material"],
            "top_k": top_k,
            "rerank": True,
        }
        results, fallback = await self.search_content(authorization, body, correlation, SEARCH_TIMEOUT)
        return read_passages(results), fallback

    async def document_titles(
        self, authorization: str, classroom_id: str, document_ids: list[str], correlation: str
    ) -> list[str]:
        """the titles of the chosen documents the caller can see in the classroom, in the order chosen; pages through content's list until all are found or it ends."""
        wanted, titles, offset = set(document_ids), dict[str, str](), 0
        while True:
            try:
                resp = await self.http.get(
                    f"{self.settings.content_url}/v1/documents/page",
                    params={"classroom_id": classroom_id, "offset": offset, "limit": DOCUMENTS_PAGE},
                    headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                    timeout=DOCUMENTS_TIMEOUT,
                )
            except httpx.HTTPError:
                raise UpstreamError("content", 0) from None
            if resp.status_code != 200:
                raise upstream_error("content", resp)
            listed, total = read_titles(resp)
            titles |= {d: t for d, t in listed.items() if d in wanted}
            offset += DOCUMENTS_PAGE - DOCUMENTS_OVERLAP
            if not listed or offset >= total or wanted <= titles.keys():
                return [titles[d] for d in document_ids if d in titles]
