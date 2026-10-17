import time
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

import learning_transport as transport
from learning_transport import auth


def test_real_jwt_validation(monkeypatch: Any) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Only remote key discovery is replaced; the actual JWT verifier is exercised.
    monkeypatch.setattr(
        auth,
        "keyset",
        lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())),
    )
    monkeypatch.setenv("OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("OIDC_AUDIENCE", "learning")
    claims = {
        "sub": "test",
        "org_id": "school",
        "iss": "https://identity.example.test",
        "aud": "learning",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }

    def request(values: dict[str, Any]) -> Request:
        token = jwt.encode(values, key, algorithm="RS256")
        return Request(
            {
                "type": "http",
                "headers": [(b"authorization", ("Bearer " + token).encode())],
            }
        )

    assert transport.actor(request(claims))["sub"] == "test"
    for update in [
        {"aud": "other"},
        {"iss": "https://evil.test"},
        {"exp": 1},
        {"org_id": ""},
    ]:
        with pytest.raises(HTTPException) as error:
            transport.actor(request({**claims, **update}))
        assert error.value.status_code == 401
    with pytest.raises(HTTPException):
        transport.actor(Request({"type": "http", "headers": [(b"x-user-id", b"test")]}))


def jwks(*keys: tuple[str, Any]) -> dict[str, Any]:
    return {"keys": [{**jwt.algorithms.RSAAlgorithm.to_jwk(k, as_dict=True), "kid": kid} for kid, k in keys]}


def test_unknown_key_ids_refetch_the_key_set_at_most_once_per_interval(monkeypatch: Any) -> None:
    old = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    new = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    served = [jwks(("old", old))]
    fetches: list[int] = []
    keys = auth.KeySet("https://identity.example.test/certs", lifespan=300)

    def fetch() -> dict[str, Any]:
        fetches.append(1)
        assert keys.jwk_set_cache is not None
        keys.jwk_set_cache.put(served[-1])
        return served[-1]

    monkeypatch.setattr(keys, "fetch_data", fetch)
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    assert keys.get_signing_key("old").key_id == "old" and len(fetches) == 1
    for _ in range(5):  # made-up key ids: one refetch, then the cached set answers
        with pytest.raises(jwt.PyJWKClientError):
            keys.get_signing_key("made-up")
    assert len(fetches) == 2
    served.append(jwks(("old", old), ("new", new)))  # the provider rotates a key in
    with pytest.raises(jwt.PyJWKClientError):
        keys.get_signing_key("new")
    clock[0] += auth.REFETCH_INTERVAL
    assert keys.get_signing_key("new").key_id == "new" and len(fetches) == 3


def test_a_little_clock_skew_is_allowed(monkeypatch: Any) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        auth,
        "keyset",
        lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())),
    )
    monkeypatch.setenv("OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("OIDC_AUDIENCE", "learning")
    now = int(time.time())
    claims = {"sub": "s", "org_id": "o", "iss": "https://identity.example.test", "aud": "learning"}

    def verified(iat: int, exp: int) -> bool:
        token = jwt.encode({**claims, "iat": iat, "exp": exp}, key, algorithm="RS256")
        try:
            transport.actor(
                Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})
            )
        except HTTPException:
            return False
        return True

    assert verified(now + 10, now + 3600)  # issued "in the future" by a clock a little ahead
    assert verified(now - 3600, now - 10)  # just expired by this clock
    assert not verified(now - 3600, now - auth.LEEWAY - 10)


@pytest.mark.parametrize(
    ("claims", "roles"),
    [
        ({"realm_access": {"roles": ["teacher", "ai-client"]}}, {"teacher", "ai-client"}),
        ({"realm_access": {"roles": "teacher"}}, {"teacher"}),
        ({"realm_access": {"roles": ["student", 7, None]}}, {"student"}),
        ({"realm_access": {"roles": {"teacher": True}}}, set()),
        ({"realm_access": ["teacher"]}, set()),
        ({"realm_access": None}, set()),
        ({}, set()),
    ],
)
def test_roles_are_read_defensively(claims: dict[str, Any], roles: set[str]) -> None:
    assert auth.Claims.from_token({"sub": "s", "org_id": "o", **claims}).roles == roles
