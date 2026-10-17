"""Render reproducible Kong OSS configuration with Keycloak's local RSA public key."""

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

BUSINESS_SERVICES = ["identity", "curriculum", "content", "assessment", "grading", "analytics"]
TIMEOUT_MS = 20_000
# Content gives an upload 5 minutes (uploadDeadline in services/content/domain.go); Kong waits a little longer, so a
# slow 50 MiB upload gets Content's answer instead of a gateway 504 while the document still commits.
CONTENT_TIMEOUT_MS = 310_000


def business_service(name: str) -> dict[str, Any]:
    timeout = CONTENT_TIMEOUT_MS if name == "content" else TIMEOUT_MS
    route: dict[str, Any] = {"name": name, "paths": [f"/api/{name}"], "strip_path": True}
    if name == "content":
        # PDFs may be up to 50 MiB (in megabytes, so 51 leaves room for the request around the file).
        route["plugins"] = [{"name": "request-size-limiting", "config": {"allowed_payload_size": 51}}]
    return {
        "name": name,
        "url": f"http://{name}:8080/v1",
        "connect_timeout": 5000,
        "read_timeout": timeout,
        "write_timeout": timeout,
        "routes": [route],
    }


# The chatbot streams its answers (server-sent events), so Kong must not buffer them.
CHATBOT = {
    "name": "question-chatbot-agent",
    "url": "http://question-chatbot-agent:8080/v1",
    "connect_timeout": 5000,
    "read_timeout": 60000,
    "write_timeout": TIMEOUT_MS,
    "routes": [
        {"name": "chatbot", "paths": ["/api/chatbot"], "strip_path": True, "response_buffering": False}
    ],
}

PLUGINS = [
    {
        "name": "jwt",
        "config": {
            "claims_to_verify": ["exp"],
            "header_names": ["authorization"],
            "uri_param_names": [],
            "cookie_names": [],
        },
    },
    {
        "name": "correlation-id",
        "config": {"header_name": "X-Correlation-ID", "generator": "uuid", "echo_downstream": True},
    },
    # Every user signs in through the one "keycloak" consumer, so limiting by consumer would give the whole school
    # one shared budget. Each signed-in session's bearer token gets its own 120 requests a minute instead.
    {
        "name": "rate-limiting",
        "config": {"minute": 120, "policy": "local", "limit_by": "header", "header_name": "authorization"},
    },
    {"name": "request-size-limiting", "config": {"allowed_payload_size": 6}},
    {
        "name": "cors",
        "config": {
            "origins": ["http://localhost:4200"],
            "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            "headers": ["Authorization", "Content-Type", "X-Correlation-ID"],
            "exposed_headers": ["X-Correlation-ID", "X-Request-ID"],
            "preflight_continue": False,
        },
    },
]


def keycloak_realm() -> dict[str, Any]:
    for _ in range(90):
        try:
            with urllib.request.urlopen("http://keycloak:8080/realms/learning", timeout=5) as response:
                realm: dict[str, Any] = json.load(response)
                return realm
        except (OSError, ValueError):
            time.sleep(2)
    raise SystemExit("Keycloak realm unavailable")


def main() -> None:
    public_key = (
        "-----BEGIN PUBLIC KEY-----\n" + keycloak_realm()["public_key"] + "\n-----END PUBLIC KEY-----"
    )
    config = {
        "_format_version": "3.0",
        "services": [*(business_service(name) for name in BUSINESS_SERVICES), CHATBOT],
        "consumers": [
            {
                "username": "keycloak",
                "jwt_secrets": [
                    {
                        "key": "http://localhost:8081/realms/learning",
                        "algorithm": "RS256",
                        "rsa_public_key": public_key,
                    }
                ],
            }
        ],
        "plugins": PLUGINS,
    }
    Path("/output/kong.json").write_text(json.dumps(config), encoding="utf-8")


if __name__ == "__main__":
    main()
