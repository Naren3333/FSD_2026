"""the teacher-token reads the agent's tools make, and inference through ai-core as the agent."""

import asyncio
from typing import Any

import httpx
from learning_transport import Claims, UpstreamError, upstream_error
from learning_transport.agents import AgentClients, billed_to

from config import Settings

READ_TIMEOUT = 10.0
# allow a bounded model turn, allowance checks and usage accounting.
GENERATE_TIMEOUT = 60.0
# grading's largest page; a student rarely has more finalized assessments in one classroom.
MAX_RESULTS = 100
# assessment's batch read takes at most 50 ids.
BATCH = 50
CLASS_PAGE = 50
CLASS_PAGES = 4  # the class comparison reads at most 200 students


def billed(claims: Claims) -> dict[str, str]:
    """whose daily ai allowance pays: the teacher who asked (adr 0003)."""
    return billed_to(claims, "teacher")


class Services(AgentClients):
    settings: Settings  # with analytics', grading's, assessment's and curriculum's urls
    generate_timeout = GENERATE_TIMEOUT

    async def _get(self, service: str, url: str, authorization: str, correlation: str, **params: Any) -> Any:
        """a 200 response's json from a business service read with the teacher's token, or upstreamerror."""
        try:
            resp = await self.http.get(
                url,
                params=params or None,
                headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                timeout=READ_TIMEOUT,
            )
        except httpx.HTTPError:
            raise UpstreamError(service, 0) from None
        if resp.status_code != 200:
            raise upstream_error(service, resp)
        try:
            return resp.json()
        except ValueError:
            raise UpstreamError(service, 502) from None

    async def performance(self, authorization: str, classroom: str, student: str, correlation: str) -> Any:
        """analytics' topic summary of the student's released grades."""
        url = f"{self.settings.analytics_url}/v1/performance/{student}"
        return await self._get("analytics", url, authorization, correlation, classroom_id=classroom)

    async def results(self, authorization: str, classroom: str, student: str, correlation: str) -> list[Any]:
        """the student's finalized evaluations in the classroom, newest first, with their answers."""
        data = await self._get(
            "grading",
            f"{self.settings.grading_url}/v1/evaluations",
            authorization,
            correlation,
            classroom_id=classroom,
            student_id=student,
            limit=MAX_RESULTS,
        )
        evaluations = data.get("evaluations") if isinstance(data, dict) else None
        if not isinstance(evaluations, list):
            raise UpstreamError("grading", 502)
        return [e for e in evaluations if isinstance(e, dict) and e.get("status") == "finalized"]

    async def assessments(self, authorization: str, ids: list[str], correlation: str) -> dict[str, Any]:
        """the teacher's view of each assessment by id; one deleted since it was graded is left out."""
        found: dict[str, Any] = {}
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            try:
                items = await self._batch(authorization, chunk, correlation)
            except UpstreamError as e:
                if e.status != 404:
                    raise
                # the batch refuses all of them when one is gone, so read them one by one.
                items = [
                    a
                    for a in await asyncio.gather(*(self._one(authorization, i, correlation) for i in chunk))
                    if a
                ]
            found.update({a["id"]: a for a in items if isinstance(a, dict) and isinstance(a.get("id"), str)})
        return found

    async def _batch(self, authorization: str, ids: list[str], correlation: str) -> list[Any]:
        try:
            resp = await self.http.post(
                f"{self.settings.assessment_url}/v1/assessments/batch",
                json={"ids": ids},
                headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                timeout=READ_TIMEOUT,
            )
        except httpx.HTTPError:
            raise UpstreamError("assessment", 0) from None
        if resp.status_code != 200:
            raise upstream_error("assessment", resp)
        try:
            items = resp.json()
        except ValueError:
            raise UpstreamError("assessment", 502) from None
        if not isinstance(items, list):
            raise UpstreamError("assessment", 502)
        return items

    async def _one(self, authorization: str, assessment: str, correlation: str) -> Any:
        url = f"{self.settings.assessment_url}/v1/assessments/{assessment}"
        try:
            return await self._get("assessment", url, authorization, correlation)
        except UpstreamError as e:
            if e.status == 404:
                return None
            raise

    async def topic_names(self, authorization: str, ids: list[str], correlation: str) -> dict[str, str]:
        """each topic's name, archived ones included; a topic curriculum no longer has is left out."""

        async def name(topic: str) -> tuple[str, str | None]:
            url = f"{self.settings.curriculum_url}/v1/topics/{topic}"
            try:
                data = await self._get("curriculum", url, authorization, correlation)
            except UpstreamError as e:
                if e.status == 404:
                    return topic, None
                raise
            value = data.get("name") if isinstance(data, dict) else None
            return topic, value if isinstance(value, str) and value else None

        pairs = await asyncio.gather(*(name(t) for t in ids))
        return {topic: value for topic, value in pairs if value}

    async def prerequisites(self, authorization: str, topic: str, correlation: str) -> list[dict[str, str]]:
        """the topic's active prerequisites, by id and name."""
        url = f"{self.settings.curriculum_url}/v1/topics/{topic}/prerequisites"
        data = await self._get("curriculum", url, authorization, correlation)
        if not isinstance(data, list):
            raise UpstreamError("curriculum", 502)
        return [
            {"id": t["id"], "name": t["name"]}
            for t in data
            if isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("name"), str)
        ]

    async def class_performance(self, authorization: str, classroom: str, correlation: str) -> list[Any]:
        """the topic summaries of the classroom's students, its first 200 in roster order."""
        students: list[Any] = []
        for page in range(CLASS_PAGES):
            data = await self._get(
                "analytics",
                f"{self.settings.analytics_url}/v1/performance",
                authorization,
                correlation,
                classroom_id=classroom,
                offset=page * CLASS_PAGE,
                limit=CLASS_PAGE,
            )
            batch = data.get("students") if isinstance(data, dict) else None
            if not isinstance(batch, list):
                raise UpstreamError("analytics", 502)
            students += batch
            if len(students) >= int(data.get("total") or 0) or not batch:
                break
        return students
