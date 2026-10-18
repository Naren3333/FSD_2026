"""Generate local-only secrets and Keycloak realm. Never overwrites an existing setup."""

import json
import secrets
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# One database and role per service that owns data. chatbot is question-chatbot-agent's (ADR 0002); aicore is
# ai-core's usage ledger (ADR 0003).
SERVICES = ["identity", "curriculum", "content", "assessment", "grading", "analytics", "chatbot", "aicore"]
# Service accounts that call the private AI services (client-credentials grant, `ai-client` role).
# grading-marker asks for AI marking suggestions (ADR 0004); question-generation-agent drafts questions and marking
# guidance (ADR 0006).
AI_CLIENTS = ["content-indexer", "question-chatbot-agent", "grading-marker", "question-generation-agent"]
# Local people: username, realm role, and the .env key holding their password.
PEOPLE = [
    ("teacher", "teacher", "TEACHER_PASSWORD"),
    ("student", "student", "STUDENT_PASSWORD"),
    ("other-teacher", "teacher", "OTHER_TEACHER_PASSWORD"),
]
ORG = "local-school"
WEB_ORIGIN = "http://localhost:4200"


def client_secret_key(client: str) -> str:
    return f"{client.upper().replace('-', '_')}_CLIENT_SECRET"


def password_key(service: str) -> str:
    """The .env key holding a service's database password."""
    return f"{service.upper()}_DB_PASSWORD"


def generate_secrets() -> dict[str, str]:
    keys = [
        "POSTGRES_PASSWORD",
        "KEYCLOAK_ADMIN_PASSWORD",
        "S3_ROOT_PASSWORD",
        *(key for _, _, key in PEOPLE),
        "S3_CONTENT_PASSWORD",
        *(client_secret_key(c) for c in AI_CLIENTS),
        *(password_key(s) for s in SERVICES),
    ]
    values = {key: secrets.token_hex(20) for key in keys}
    values["S3_ROOT_USER"] = "local-admin"
    values["S3_CONTENT_USER"] = "content-service"
    return values


def postgres_init(values: dict[str, str]) -> str:
    """Each service gets its own login role and database; no one else may connect to it."""
    sql = []
    for name in SERVICES:
        password = values[password_key(name)]
        sql += [
            f"CREATE ROLE {name} LOGIN PASSWORD '{password}';",
            f"CREATE DATABASE {name} OWNER {name};",
            f"REVOKE ALL ON DATABASE {name} FROM PUBLIC;",
        ]
    # pgvector is not a trusted extension, so the superuser creates it for Content (its embeddings index).
    sql += ["\\connect content", "CREATE EXTENSION IF NOT EXISTS vector;"]
    return "\n".join(sql)


def person(username: str, role: str, password: str) -> dict[str, Any]:
    return {
        "username": username,
        "enabled": True,
        "emailVerified": True,
        "firstName": username.title(),
        "lastName": "Local",
        "email": f"{username}@example.test",
        "attributes": {"org_id": [ORG]},
        "realmRoles": [role],
        "credentials": [{"type": "password", "value": password, "temporary": False}],
    }


def service_account(client: str) -> dict[str, Any]:
    return {
        "username": f"service-account-{client}",
        "enabled": True,
        "serviceAccountClientId": client,
        "realmRoles": ["ai-client"],
    }


AUDIENCE = {
    "name": "learning-audience",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {"included.custom.audience": "learning", "access.token.claim": "true"},
}
# People carry their organization as a user attribute.
PERSON_MAPPERS = [
    AUDIENCE,
    {
        "name": "organization",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "config": {
            "user.attribute": "org_id",
            "claim.name": "org_id",
            "jsonType.label": "String",
            "access.token.claim": "true",
            "id.token.claim": "true",
        },
    },
]
# A service account has no user attributes to map, so its organization is a fixed claim. One local
# organization only: a multi-organization deployment needs a per-request delegated identity instead.
SERVICE_MAPPERS = [
    AUDIENCE,
    {
        "name": "organization",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {
            "claim.name": "org_id",
            "claim.value": ORG,
            "jsonType.label": "String",
            "access.token.claim": "true",
        },
    },
]


def clients(values: dict[str, str]) -> list[dict[str, Any]]:
    web = {
        "clientId": "learning-web",
        "publicClient": True,
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "redirectUris": [f"{WEB_ORIGIN}/*"],
        "webOrigins": [WEB_ORIGIN],
        "attributes": {"pkce.code.challenge.method": "S256"},
        "protocolMappers": PERSON_MAPPERS,
    }
    # Password grant for the integration and browser tests only.
    e2e = {
        "clientId": "learning-e2e",
        "publicClient": True,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": True,
        "protocolMappers": PERSON_MAPPERS,
    }
    services = [service_client(client, values[client_secret_key(client)]) for client in AI_CLIENTS]
    return [web, e2e, *services]


def service_client(client: str, secret: str) -> dict[str, Any]:
    """A confidential client-credentials client for a service account."""
    return {
        "clientId": client,
        "publicClient": False,
        "secret": secret,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": True,
        "protocolMappers": SERVICE_MAPPERS,
    }


def realm(values: dict[str, str]) -> dict[str, Any]:
    users = [person(username, role, values[key]) for username, role, key in PEOPLE]
    users += [service_account(client) for client in AI_CLIENTS]
    return {
        "realm": "learning",
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "roles": {"realm": [{"name": r} for r in ["teacher", "student", "ai-client"]]},
        "users": users,
        "clients": clients(values),
    }


def env_file(values: dict[str, str]) -> str:
    # Provider keys are the developer's own: left empty here, and ai-core answers 503 until they are set.
    providers = "\n# AI provider keys, used only by ai-core (ADR 0002). Fill in by hand.\nOPENAI_API_KEY=\nVOYAGE_API_KEY=\n"
    lines = "\n".join(f"{k}={v}" for k, v in values.items())
    return "# Generated local development secrets. Never commit.\n" + lines + "\n" + providers


def main() -> None:
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    if (ROOT / ".env").exists():
        raise SystemExit(".env already exists; preserving existing credentials. See README for recovery.")
    values = generate_secrets()
    (local / "postgres-init.sql").write_text(postgres_init(values), encoding="utf-8")
    (local / "learning-realm.json").write_text(json.dumps(realm(values), indent=2), encoding="utf-8")
    (ROOT / ".env").write_text(env_file(values), encoding="utf-8")
    print("Created .env and .local infrastructure configuration. Local user passwords are in .env.")


if __name__ == "__main__":
    main()
