import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
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
        lambda: SimpleNamespace(signing_key=lambda _: SimpleNamespace(key=key.public_key())),
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


def token_for(kid: str) -> str:
    """A token whose header names kid; key lookup reads only the header."""
    return jwt.encode({"sub": "s"}, "secret", algorithm="HS256", headers={"kid": kid})


def test_unknown_key_ids_refetch_the_key_set_at_most_once_per_interval(monkeypatch: Any) -> None:
    old = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    new = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    served = [jwks(("old", old))]
    fetches: list[int] = []

    def fetch() -> dict[str, Any]:
        fetches.append(1)
        return served[-1]

    keys = auth.KeySet("https://identity.example.test/certs", lifespan=300, fetch=fetch)
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    assert keys.signing_key(token_for("old")).key_id == "old" and len(fetches) == 1
    for _ in range(5):  # made-up key ids: one refetch, then the cached set answers
        with pytest.raises(jwt.PyJWTError):
            keys.signing_key(token_for("made-up"))
    assert len(fetches) == 2
    served.append(jwks(("old", old), ("new", new)))  # the provider rotates a key in
    with pytest.raises(jwt.PyJWTError):
        keys.signing_key(token_for("new"))
    clock[0] += auth.REFETCH_INTERVAL
    assert keys.signing_key(token_for("new")).key_id == "new" and len(fetches) == 3
    clock[0] += 300  # the cached set expires: known key ids refetch it too
    assert keys.signing_key(token_for("old")).key_id == "old" and len(fetches) == 4


def test_a_key_set_that_cannot_be_fetched_refuses_the_token() -> None:
    def fetch() -> Any:
        raise httpx.ConnectError("down")

    for broken in [fetch, lambda: ["not", "a", "set"], lambda: {"keys": []}]:
        with pytest.raises(jwt.PyJWTError):
            auth.KeySet("https://identity.example.test/certs", fetch=broken).signing_key(token_for("k"))


def test_an_outage_keeps_the_keys_held_and_is_not_retried_on_every_request(monkeypatch: Any) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    up = [True]
    fetches: list[int] = []

    def fetch() -> dict[str, Any]:
        fetches.append(1)
        if not up[0]:
            raise httpx.ConnectError("down")
        return jwks(("k", key))

    keys = auth.KeySet("https://identity.example.test/certs", lifespan=300, fetch=fetch)
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    assert keys.signing_key(token_for("k")).key_id == "k"
    up[0] = False
    clock[0] += 300  # the set is due a refetch, which fails: the held key still verifies
    for _ in range(5):
        assert keys.signing_key(token_for("k")).key_id == "k"
    assert len(fetches) == 2
    clock[0] += auth.REFETCH_INTERVAL
    up[0] = True
    assert keys.signing_key(token_for("k")).key_id == "k" and len(fetches) == 3


def test_a_little_clock_skew_is_allowed(monkeypatch: Any) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        auth,
        "keyset",
        lambda: SimpleNamespace(signing_key=lambda _: SimpleNamespace(key=key.public_key())),
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


def test_tokens_not_signed_with_the_realms_rsa_key_are_refused(monkeypatch: Any) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # The real key set, served locally.
    keys = auth.KeySet("https://identity.example.test/certs", fetch=lambda: jwks(("realm", key.public_key())))
    monkeypatch.setattr(auth, "keyset", lambda: keys)
    monkeypatch.setenv("OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("OIDC_AUDIENCE", "learning")
    now = int(time.time())
    claims = {
        "sub": "s",
        "org_id": "o",
        "iss": "https://identity.example.test",
        "aud": "learning",
        "iat": now,
        "exp": now + 3600,
    }
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    def segment(value: dict[str, Any]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")

    def token(alg: str, signing_key: bytes) -> str:
        """A token with header `alg`, signed with HMAC-SHA256 under signing_key when one is given."""
        body = segment({"alg": alg, "kid": "realm", "typ": "JWT"}) + b"." + segment(claims)
        mac = hmac.new(signing_key, body, hashlib.sha256).digest() if signing_key else b""
        return (body + b"." + base64.urlsafe_b64encode(mac).rstrip(b"=")).decode()

    def status(bearer: str) -> int:
        try:
            transport.actor(
                Request({"type": "http", "headers": [(b"authorization", f"Bearer {bearer}".encode())]})
            )
        except HTTPException as error:
            return error.status_code
        return 200

    assert status(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "realm"})) == 200
    assert status(token("none", b"")) == 401
    assert status(token("HS256", public_pem)) == 401
    assert status(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "other"})) == 401
