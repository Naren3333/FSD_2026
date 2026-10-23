"""render reproducible kong oss configuration with keycloak's local rsa public key."""

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

BUSINESS_SERVICES = ["identity", "curriculum", "content", "assessment", "grading", "analytics"]
TIMEOUT_MS = 20_000
# let the upload service finish before the gateway times out.
CONTENT_TIMEOUT_MS = 310_000
# the token issuer and the web app's origin, on the ports the compose stack publishes (docker-compose.yml).
ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8081/realms/learning")
WEB_ORIGIN = os.environ.get("WEB_ORIGIN", "http://localhost:4200")


def business_service(name: str) -> dict[str, Any]:
    timeout = CONTENT_TIMEOUT_MS if name == "content" else TIMEOUT_MS
    route: dict[str, Any] = {"name": name, "paths": [f"/api/{name}"], "strip_path": True}
    if name == "content":
        # pdfs may be up to 50 mib (in megabytes, so 51 leaves room for the request around the file).
        route["plugins"] = [{"name": "request-size-limiting", "config": {"allowed_payload_size": 51}}]
    return {
        "name": name,
        "url": f"http://{name}:8080/v1",
        "connect_timeout": 5000,
        "read_timeout": timeout,
        "write_timeout": timeout,
        "routes": [route],
    }


# the chatbot streams its answers (server-sent events), so kong must not buffer them.
CHATBOT = {
    "name": "question-chatbot-agent",
    "url": "http://question-chatbot-agent:8080/v1",
    "connect_timeout": 5000,
    "read_timeout": 150_000,
    "write_timeout": TIMEOUT_MS,
    "routes": [
        {"name": "chatbot", "paths": ["/api/chatbot"], "strip_path": True, "response_buffering": False}
    ],
}

# allow generation to finish its search, model call and accounting before the gateway times out.
QUESTION_GENERATION = {
    "name": "question-generation-agent",
    "url": "http://question-generation-agent:8080/v1",
    "connect_timeout": 5000,
    "read_timeout": 180_000,
    "write_timeout": TIMEOUT_MS,
    "routes": [{"name": "question-generation", "paths": ["/api/question-generation"], "strip_path": True}],
}

# performance interpretation runs several bounded model turns (adr 0014): its deadline forces a report after
# two minutes, and the last turns may take a minute each.
PERFORMANCE_AGENT = {
    "name": "student-performance-agent",
    "url": "http://student-performance-agent:8080/v1",
    "connect_timeout": 5000,
    "read_timeout": 300_000,
    "write_timeout": TIMEOUT_MS,
    "routes": [{"name": "performance-agent", "paths": ["/api/performance-agent"], "strip_path": True}],
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
    # every user signs in through the one "keycloak" consumer, so limiting by consumer would give the whole
    # school one shared budget.
    {
        "name": "rate-limiting",
        "config": {"minute": 120, "policy": "local", "limit_by": "header", "header_name": "authorization"},
    },
    {"name": "request-size-limiting", "config": {"allowed_payload_size": 6}},
    {
        "name": "cors",
        "config": {
            "origins": [WEB_ORIGIN],
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
        "services": [
            *(business_service(name) for name in BUSINESS_SERVICES),
            CHATBOT,
            QUESTION_GENERATION,
            PERFORMANCE_AGENT,
        ],
        "consumers": [
            {
                "username": "keycloak",
                "jwt_secrets": [
                    {
                        "key": ISSUER,
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
