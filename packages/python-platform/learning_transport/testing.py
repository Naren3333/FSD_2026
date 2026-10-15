"""Test doubles for verified token claims, for the services' tests only: never imported by an application."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI

from learning_transport.auth import actor


def token_claims(*roles: str, sub: str = "test-user", org: str = "test-org") -> dict[str, Any]:
    """Claims as `actor` returns them for a verified token holding `roles`."""
    return {"sub": sub, "org_id": org, "realm_access": {"roles": list(roles)}}


@contextmanager
def as_caller(
    app: FastAPI,
    claims: dict[str, Any] | Callable[..., dict[str, Any]],
    dependency: Callable[..., Any] = actor,
) -> Iterator[None]:
    """Inside the block, `app` treats requests as carrying a verified token with `claims` (or the claims a
    dependency function returns, which may read the request). The override is removed afterwards."""
    app.dependency_overrides[dependency] = claims if callable(claims) else lambda: claims
    try:
        yield
    finally:
        app.dependency_overrides.pop(dependency, None)
