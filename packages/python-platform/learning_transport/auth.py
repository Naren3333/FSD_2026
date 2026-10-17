"""Bearer-token verification and the caller's identity. Identity comes only from a verified token, never from
headers or request bodies."""

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, Request

from learning_transport.errors import failure

TEACHER, STUDENT, AI_CLIENT = "teacher", "student", "ai-client"
LEEWAY = 30  # seconds of clock skew allowed between the identity provider and this service (exp, iat, nbf)
REFETCH_INTERVAL = 30.0  # seconds between key set refetches for a key id the cached set lacks


class KeySet(jwt.PyJWKClient):
    """PyJWKClient refetching the key set for an unknown key id at most once per REFETCH_INTERVAL. The stock
    client refetches on every such token, so tokens with made-up key ids would each wait up to its timeout on the
    identity provider (and hammer it); a key the provider really rotated in is still picked up within the
    interval."""

    def __init__(self, uri: str, **kwargs: Any) -> None:
        super().__init__(uri, **kwargs)
        self._refetch_lock = threading.Lock()
        self._refetched = -REFETCH_INTERVAL

    def get_signing_key(self, kid: str) -> jwt.PyJWK:
        key = self.match_kid(self.get_signing_keys(), kid)
        if key is None:
            with self._refetch_lock:  # one refetch at a time; the others then see its result
                refresh = time.monotonic() - self._refetched >= REFETCH_INTERVAL
                if refresh:
                    self._refetched = time.monotonic()
                key = self.match_kid(self.get_signing_keys(refresh=refresh), kid)
        if key is None:
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return key


@lru_cache
def keyset() -> jwt.PyJWKClient:
    return KeySet(os.environ["OIDC_JWKS_URL"], timeout=5, lifespan=300)


def actor(request: Request) -> dict[str, Any]:
    """The verified claims of the request's bearer token: signature, issuer, audience and expiry are checked,
    and `sub` and `org_id` must be present. 401 otherwise."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise failure(401, "UNAUTHENTICATED", "Sign in to continue.")
    try:
        token = auth[7:]
        key = keyset().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=os.environ["OIDC_AUDIENCE"],
            issuer=os.environ["OIDC_ISSUER"],
            options={"require": ["exp", "iat", "sub", "org_id"]},
            leeway=LEEWAY,
        )
    except (jwt.PyJWTError, jwt.PyJWKClientError):
        raise failure(401, "UNAUTHENTICATED", "The session is invalid or expired.") from None
    if not claims.get("sub") or not claims.get("org_id"):
        raise failure(401, "UNAUTHENTICATED", "Identity claims are missing.")
    return dict(claims)


@dataclass(frozen=True)
class Claims:
    """The caller, from verified token claims."""

    subject: str
    org: str
    roles: frozenset[str]

    @classmethod
    def from_token(cls, claims: Mapping[str, Any]) -> "Claims":
        """Roles from `realm_access.roles`: a list of names, or one name; anything else holds no role."""
        access = claims.get("realm_access")
        roles = access.get("roles") if isinstance(access, Mapping) else None
        if isinstance(roles, str):
            roles = [roles]
        names = frozenset(r for r in roles if isinstance(r, str)) if isinstance(roles, list) else frozenset()
        return cls(str(claims.get("sub", "")), str(claims.get("org_id", "")), names)

    def has(self, role: str) -> bool:
        return role in self.roles

    @property
    def person_role(self) -> str | None:
        """The person's role: teacher (which wins for an account with both) or student; None for a service
        account."""
        if self.has(TEACHER):
            return TEACHER
        if self.has(STUDENT):
            return STUDENT
        return None


def caller(claims: dict[str, Any] = Depends(actor)) -> Claims:
    """FastAPI dependency: the verified caller."""
    return Claims.from_token(claims)


def requires(*roles: str) -> Callable[..., Claims]:
    """FastAPI dependency factory: the verified caller, who must hold at least one of `roles` (403 otherwise)."""

    def dependency(claims: Claims = Depends(caller)) -> Claims:
        if not any(claims.has(role) for role in roles):
            raise failure(403, "FORBIDDEN", "Your role cannot perform this operation.")
        return claims

    return dependency
