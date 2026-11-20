"""test doubles for verified token claims, for the services' tests only: never imported by an application."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, Request

from learning_transport.auth import actor


def token_claims(*roles: str, sub: str = "test-user", org: str = "test-org") -> dict[str, Any]:
    """claims as `actor` returns them for a verified token holding `roles`."""
    return {"sub": sub, "org_id": org, "realm_access": {"roles": list(roles)}}


def user_actor(request: Request) -> dict[str, Any]:
    """an `actor` override for the user the test's bearer token names (see as_user): t… is a teacher, a… a service account, x… holds no person role, anyone else is a student."""
    user = request.headers["authorization"].removeprefix("Bearer ")
    role = {"t": "teacher", "a": "ai-client", "x": "parent"}.get(user[:1], "student")
    return token_claims(role, sub=user, org="org")


def as_user(user: str) -> dict[str, str]:
    """the authorization header naming a test user for user_actor."""
    return {"Authorization": f"Bearer {user}"}


@contextmanager
def as_caller(
    app: FastAPI,
    claims: dict[str, Any] | Callable[..., dict[str, Any]],
    dependency: Callable[..., Any] = actor,
) -> Iterator[None]:
    """inside the block, `app` treats requests as carrying a verified token with `claims` (or the claims a dependency function returns, which may read the request)."""
    app.dependency_overrides[dependency] = claims if callable(claims) else lambda: claims
    try:
        yield
    finally:
        app.dependency_overrides.pop(dependency, None)
