"""the teacher-token reads the agent's tools make, and inference through ai-core as the agent."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from learning_transport import TEACHER, Claims, UpstreamError, upstream_error
from learning_transport.agents import AgentClients, billed_to, gather_all

import upstream
from config import Settings

READ_TIMEOUT = 10.0
# ai-core's budget for the 3000-token report (70 s), plus its allowance checks and usage accounting.
GENERATE_TIMEOUT = 75.0
# reads of one topic or assessment each that run at once.
FAN_OUT = 8
# grading's largest page, and how many pages are read at most looking for finalized results among drafts.
EVALUATION_LIMIT = 100
EVALUATION_PAGES = 5
# assessment's batch read takes at most 50 ids.
BATCH = 50
CLASS_PAGE = 50
CLASS_PAGES = 4  # the class comparison reads at most 200 students


@dataclass(frozen=True)
class ClassSample:
    students: list[upstream.ClassStudent]
    total: int


def billed(claims: Claims) -> dict[str, str]:
    """whose daily ai allowance pays: the teacher who asked (adr 0003)."""
    return billed_to(claims, TEACHER)


class Services(AgentClients):
    settings: Settings  # with analytics', grading's, assessment's and curriculum's urls
    generate_timeout = GENERATE_TIMEOUT

    async def _get(self, service: str, url: str, authorization: str, correlation: str, **params: Any) -> Any:
        return await self._call("GET", service, url, authorization, correlation, params=params or None)

    async def _post(self, service: str, url: str, authorization: str, correlation: str, body: Any) -> Any:
        return await self._call("POST", service, url, authorization, correlation, json=body)

    async def _call(
        self, method: str, service: str, url: str, authorization: str, correlation: str, **request: Any
    ) -> Any:
        """a 200 response's json from a business service called with the teacher's token, or upstreamerror."""
        try:
            resp = await self.http.request(
                method,
                url,
                headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                timeout=READ_TIMEOUT,
                **request,
            )
        except httpx.HTTPError:
            raise UpstreamError(service, 0) from None
        if resp.status_code != 200:
            raise upstream_error(service, resp)
        try:
            return resp.json()
        except ValueError:
            raise UpstreamError(service, 502) from None

    async def performance(
        self, authorization: str, classroom: str, student: str, correlation: str
    ) -> upstream.Performance:
        """analytics' topic summary of the student's released grades (finalized ones not yet released are not in it)."""
        url = f"{self.settings.analytics_url}/v1/performance/{student}"
        return upstream.read(
            upstream.performance,
            await self._get("analytics", url, authorization, correlation, classroom_id=classroom),
            "analytics",
        )

    async def results(
        self, authorization: str, classroom: str, student: str, correlation: str, wanted: int
    ) -> list[upstream.Evaluation]:
        """the student's newest `wanted` finalized evaluations in the classroom, newest first, with their answers. grading lists drafts too, so pages are read until enough are finalized or none are left."""
        finalized: list[upstream.Evaluation] = []
        for n in range(EVALUATION_PAGES):
            data = await self._get(
                "grading",
                f"{self.settings.grading_url}/v1/evaluations",
                authorization,
                correlation,
                classroom_id=classroom,
                student_id=student,
                offset=n * EVALUATION_LIMIT,
                limit=EVALUATION_LIMIT,
            )
            data = upstream.read(upstream.evaluation_page, data, "grading")
            evaluations = data["evaluations"]
            page = [e for e in evaluations if e["status"] == "finalized"]
            finalized += page
            total = data["total"]
            if (
                len(finalized) >= wanted
                or len(evaluations) < EVALUATION_LIMIT
                or (n + 1) * EVALUATION_LIMIT >= total
            ):
                break
        return finalized[:wanted]

    async def assessments(
        self, authorization: str, ids: list[str], correlation: str
    ) -> dict[str, upstream.Assessment]:
        """the teacher's view of each assessment by id; one deleted since it was graded is left out."""
        found: dict[str, upstream.Assessment] = {}
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            try:
                items = await self._batch(authorization, chunk, correlation)
            except UpstreamError as e:
                if e.status != 404:
                    raise
                # the batch refuses all of them when one is gone, so read them one by one.
                read = await bounded(lambda i: self._one(authorization, i, correlation), chunk)
                items = [a for a in read if a]
            found.update({a["id"]: a for a in items})
        return found

    async def _batch(self, authorization: str, ids: list[str], correlation: str) -> list[upstream.Assessment]:
        url = f"{self.settings.assessment_url}/v1/assessments/batch"
        items = await self._post("assessment", url, authorization, correlation, {"ids": ids})
        return upstream.read(upstream.assessments, items, "assessment")

    async def _one(self, authorization: str, assessment: str, correlation: str) -> upstream.Assessment | None:
        url = f"{self.settings.assessment_url}/v1/assessments/{assessment}"
        try:
            return upstream.read(
                upstream.assessment,
                await self._get("assessment", url, authorization, correlation),
                "assessment",
            )
        except UpstreamError as e:
            if e.status == 404:
                return None
            raise

    async def topic_names(self, authorization: str, ids: list[str], correlation: str) -> dict[str, str]:
        """each topic's name, archived ones included; a topic curriculum no longer has is left out."""

        found: dict[str, str] = {}
        unique = list(dict.fromkeys(ids))
        for start in range(0, len(unique), BATCH):
            chunk = unique[start : start + BATCH]
            url = f"{self.settings.curriculum_url}/v1/topics/batch"
            items = await self._post("curriculum", url, authorization, correlation, {"ids": chunk})
            items = upstream.read(upstream.topic_names, items, "curriculum")
            if any(item["id"] not in chunk or not item["name"] for item in items):
                raise UpstreamError("curriculum", 502)
            found.update({item["id"]: item["name"] for item in items})
        return found

    async def prerequisites(
        self, authorization: str, topic: str, correlation: str
    ) -> list[upstream.TopicName]:
        """the topic's active prerequisites, by id and name."""
        url = f"{self.settings.curriculum_url}/v1/topics/{topic}/prerequisites"
        data = await self._get("curriculum", url, authorization, correlation)
        return upstream.read(upstream.topic_names, data, "curriculum")

    async def class_performance(self, authorization: str, classroom: str, correlation: str) -> ClassSample:
        """the topic summaries of the classroom's students, its first 200 in roster order."""

        async def page(n: int) -> tuple[list[upstream.ClassStudent], int]:
            data = await self._get(
                "analytics",
                f"{self.settings.analytics_url}/v1/performance",
                authorization,
                correlation,
                classroom_id=classroom,
                offset=n * CLASS_PAGE,
                limit=CLASS_PAGE,
            )
            data = upstream.read(upstream.class_page, data, "analytics")
            batch, total = data["students"], data["total"]
            if total < len(batch):
                raise UpstreamError("analytics", 502)
            return batch, total

        # the first page gives the total; the rest are read side by side.
        students, total = await page(0)
        if not students:
            return ClassSample(students, total)
        pages = min(CLASS_PAGES, -(-total // CLASS_PAGE))
        for batch, population in await gather_all(*(page(n) for n in range(1, pages))):
            students += batch
            total = max(total, population)
        unique = {s["student_id"]: s for s in students}
        return ClassSample(list(unique.values()), total)


async def bounded[T, R](read: Callable[[T], Awaitable[R]], items: list[T]) -> list[R]:
    """`read` of every item, at most FAN_OUT at once; the first failure cancels the rest."""
    slots = asyncio.Semaphore(FAN_OUT)

    async def one(item: T) -> R:
        async with slots:
            return await read(item)

    return await gather_all(*(one(i) for i in items))
