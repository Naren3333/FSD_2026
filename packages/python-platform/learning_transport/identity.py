"""Classroom access, decided by the Identity service. Callers forward the user's own token, so Identity's rules
decide; a service never works out classroom membership itself."""

from collections.abc import Collection

import httpx
from fastapi import Request

from learning_transport.errors import UpstreamError, failure

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

    async def roster(self, authorization: str, classroom_id: str, correlation: str) -> list[str] | None:
        """The ids of all the classroom's students, in Identity's order (by name), for its own teacher: None when
        access is denied. Raises UpstreamError when Identity is unavailable or answers out of contract."""
        try:
            resp = await self.http.get(
                f"{self.base_url}/v1/classrooms/{classroom_id}/student-ids",
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
            ids = resp.json()
        except ValueError:
            raise UpstreamError("identity", 502) from None
        if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
            raise UpstreamError("identity", 502)
        return ids


async def require_relation(
    identity: IdentityClient,
    request: Request,
    classroom_id: str,
    *,
    student_id: str | None = None,
    allowed: Collection[str] = (RELATION_TEACHER, RELATION_STUDENT),
    denied: tuple[str, str] = ("FORBIDDEN", "You do not have access to this classroom."),
) -> str:
    """The caller's relation to the classroom (optionally on behalf of a student), as Identity decides it with the
    request's own token. 503 when Identity is unavailable; 403 with the `denied` code and message when access is
    denied or the relation is not one of `allowed`."""
    authorization, correlation = request.headers["authorization"], request.state.correlation_id
    try:
        relation = await identity.relation(authorization, classroom_id, correlation, student_id)
    except UpstreamError:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "Authorization is unavailable; retry later.") from None
    if relation is None or relation not in allowed:
        raise failure(403, *denied)
    return relation
