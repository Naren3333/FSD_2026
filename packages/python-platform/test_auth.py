import time
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request
import learning_transport as transport


def test_real_jwt_validation(monkeypatch: Any) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Only remote key discovery is replaced; the actual JWT verifier is exercised.
    monkeypatch.setattr(
        transport,
        "keyset",
        lambda: SimpleNamespace(
            get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())
        ),
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
