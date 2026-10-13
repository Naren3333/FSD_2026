"""Classroom access, decided by the Identity service. Callers forward the user's own token, so Identity's rules
decide; a service never works out classroom membership itself."""

import httpx

from learning_transport.errors import UpstreamError

RELATION_TEACHER, RELATION_STUDENT = "teacher", "student"


class IdentityClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self.base_url, self.http = base_url.rstrip("/"), http

    async def relation(
        self, authorization: str, classroom_id: str, correlation: str, student_id: str | None = None
    ) -> str | None:
        """How the caller may access the classroom (optionally on behalf of a student): RELATION_TEACHER for its
        own teacher, RELATION_STUDENT for an enrolled student, None when access is denied. Raises UpstreamError
        when Identity is unavailable."""
        try:
            resp = await self.http.get(
                f"{self.base_url}/v1/classrooms/{classroom_id}/access",
                params={"student_id": student_id} if student_id else None,
                headers={"Authorization": authorization, "X-Correlation-ID": correlation},
                timeout=5,
            )
        except httpx.HTTPError:
            raise UpstreamError("identity", 0) from None
        if resp.status_code >= 500:
            raise UpstreamError("identity", resp.status_code)
        if resp.status_code != 200:
            return None
        try:
            relation = resp.json().get("relation")
        except ValueError:
            raise UpstreamError("identity", 502) from None
        if relation not in (RELATION_TEACHER, RELATION_STUDENT):
            raise UpstreamError("identity", 502)
        return str(relation)
