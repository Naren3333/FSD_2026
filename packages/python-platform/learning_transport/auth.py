"""Bearer-token verification and the caller's identity. Identity comes only from a verified token, never from
headers or request bodies."""

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
import jwt
from fastapi import Depends, Request

from learning_transport.errors import failure

TEACHER, STUDENT, AI_CLIENT = "teacher", "student", "ai-client"
LEEWAY = 30  # seconds of clock skew allowed between the identity provider and this service (exp, iat, nbf)
REFETCH_INTERVAL = 30.0  # seconds between key set refetches for a key id the cached set lacks

log = logging.getLogger("learning_transport.auth")


class KeySet:
    """The identity provider's signing keys (its JWKS), cached for `lifespan` seconds. A token naming a key id the
    cached set lacks refetches the set at most once per REFETCH_INTERVAL, so tokens with made-up key ids don't each
    wait on the identity provider (and hammer it), while a key the provider really rotated in is still picked up
    within the interval. A failed fetch keeps the keys already held and is retried no sooner than REFETCH_INTERVAL,
    so an identity provider outage doesn't make every request wait on it. `fetch` replaces the HTTP request in
    tests."""

    def __init__(
        self,
        uri: str,
        *,
        timeout: float = 5,
        lifespan: float = 300,
        fetch: Callable[[], Any] | None = None,
    ) -> None:
        self._fetch = fetch or (lambda: httpx.get(uri, timeout=timeout).raise_for_status().json())
        self._lifespan = lifespan
        self._lock = threading.Lock()  # one fetch at a time; the others then see its result
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched: float | None = None
        self._refetched = -REFETCH_INTERVAL

    def signing_key(self, token: str) -> jwt.PyJWK:
        """The key that signed token, by the key id in its (unverified) header. PyJWTError when there is none."""
        kid = jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str):
            raise jwt.InvalidTokenError("The token names no signing key.")
        key = self._keys.get(kid)
        if key is None or self._stale(time.monotonic()):
            with self._lock:  # one fetch at a time; the others then see its result
                now = time.monotonic()
                if self._stale(now):
                    self._load(now)
                elif kid not in self._keys and now - self._refetched >= REFETCH_INTERVAL:
                    self._refetched = now
                    self._load(now)
                key = self._keys.get(kid)
        if key is None:
            raise jwt.InvalidTokenError(f"No signing key matches {kid!r}.")
        return key

    def _stale(self, now: float) -> bool:
        return self._fetched is None or now - self._fetched >= self._lifespan

    def _load(self, now: float) -> None:
        try:
            data = self._fetch()
            if not isinstance(data, dict):
                raise jwt.PyJWKSetError("the key set is not a JSON object")
            keys = jwt.PyJWKSet.from_dict(data).keys
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as error:
            log.warning("fetching the identity provider's key set failed: %s", error)
            # Try again after REFETCH_INTERVAL, not on every request meanwhile.
            self._fetched = now - self._lifespan + REFETCH_INTERVAL
            return
        self._keys = {k.key_id: k for k in keys if k.key_id and k.public_key_use in ("sig", None)}
        self._fetched = now


@lru_cache
def keyset() -> KeySet:
    return KeySet(os.environ["OIDC_JWKS_URL"], timeout=5, lifespan=300)


def actor(request: Request) -> dict[str, Any]:
    """The verified claims of the request's bearer token: signature, issuer, audience and expiry are checked,
    and `sub` and `org_id` must be present. 401 otherwise."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise failure(401, "UNAUTHENTICATED", "Sign in to continue.")
    try:
        token = auth[7:]
        key = keyset().signing_key(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=os.environ["OIDC_AUDIENCE"],
            issuer=os.environ["OIDC_ISSUER"],
            options={"require": ["exp", "iat", "sub", "org_id"]},
            leeway=LEEWAY,
        )
    except jwt.PyJWTError:
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
        """The person's role, teacher or student (an account never holds both; see caller); None for a service
        account."""
        if self.has(TEACHER):
            return TEACHER
        if self.has(STUDENT):
            return STUDENT
        return None


def caller(claims: dict[str, Any] = Depends(actor)) -> Claims:
    """FastAPI dependency: the verified caller. An account is a teacher or a student, never both: a token holding
    both roles is refused with 403."""
    verified = Claims.from_token(claims)
    if verified.has(TEACHER) and verified.has(STUDENT):
        raise failure(403, "ROLE_CONFLICT", "An account must be either a teacher or a student, not both.")
    return verified


def requires(*roles: str) -> Callable[..., Claims]:
    """FastAPI dependency factory: the verified caller, who must hold at least one of `roles` (403 otherwise)."""

    def dependency(claims: Claims = Depends(caller)) -> Claims:
        if not any(claims.has(role) for role in roles):
            raise failure(403, "FORBIDDEN", "Your role cannot perform this operation.")
        return claims

    return dependency
