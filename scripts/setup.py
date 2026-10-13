"""Generate local-only secrets and Keycloak realm. Never overwrites an existing setup."""
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["identity", "curriculum", "content", "assessment", "grading", "analytics", "chatbot", "aicore"]  # databases; chatbot is question-chatbot-agent's (ADR 0002), aicore is ai-core's usage ledger (ADR 0003)
# Service accounts that call the private AI services (client-credentials grant, `ai-client` role).
AI_CLIENTS = ["content-indexer", "question-chatbot-agent"]


def main() -> None:
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    if (ROOT / ".env").exists():
        raise SystemExit(".env already exists; preserving existing credentials. See README for recovery.")
    values = {key: secrets.token_hex(20) for key in ["POSTGRES_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", "S3_ROOT_PASSWORD", "TEACHER_PASSWORD", "STUDENT_PASSWORD", "OTHER_TEACHER_PASSWORD", "S3_CONTENT_PASSWORD", *[f"{c.upper().replace('-', '_')}_CLIENT_SECRET" for c in AI_CLIENTS], *[f"{s.upper()}_DB_PASSWORD" for s in SERVICES]]}
    values["S3_ROOT_USER"] = "local-admin"
    values["S3_CONTENT_USER"] = "content-service"
    sql = []
    for name in SERVICES:
        password = values[f"{name.upper()}_DB_PASSWORD"]
        sql += [f"CREATE ROLE {name} LOGIN PASSWORD '{password}';", f"CREATE DATABASE {name} OWNER {name};", f"REVOKE ALL ON DATABASE {name} FROM PUBLIC;"]
    # pgvector is not a trusted extension, so the superuser creates it for Content (its embeddings index).
    sql += ["\\connect content", "CREATE EXTENSION IF NOT EXISTS vector;"]
    (local / "postgres-init.sql").write_text("\n".join(sql), encoding="utf-8")
    users = []
    for username, role, key in [("teacher", "teacher", "TEACHER_PASSWORD"), ("student", "student", "STUDENT_PASSWORD"), ("other-teacher", "teacher", "OTHER_TEACHER_PASSWORD")]:
        users.append({"username": username, "enabled": True, "emailVerified": True, "firstName": username.title(), "lastName": "Local", "email": f"{username}@example.test", "attributes": {"org_id": ["local-school"]}, "realmRoles": [role], "credentials": [{"type": "password", "value": values[key], "temporary": False}]})
    mappers = [{"name": "learning-audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper", "config": {"included.custom.audience": "learning", "access.token.claim": "true"}}, {"name": "organization", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-attribute-mapper", "config": {"user.attribute": "org_id", "claim.name": "org_id", "jsonType.label": "String", "access.token.claim": "true", "id.token.claim": "true"}}]
    # A service account has no user attributes to map, so its organization is a fixed claim. One local
    # organization only: a multi-organization deployment needs a per-request delegated identity instead.
    service_mappers = [mappers[0], {"name": "organization", "protocol": "openid-connect", "protocolMapper": "oidc-hardcoded-claim-mapper", "config": {"claim.name": "org_id", "claim.value": "local-school", "jsonType.label": "String", "access.token.claim": "true"}}]
    for client in AI_CLIENTS:
        users.append({"username": f"service-account-{client}", "enabled": True, "serviceAccountClientId": client, "realmRoles": ["ai-client"]})
    service_clients = [{"clientId": c, "publicClient": False, "secret": values[f"{c.upper().replace('-', '_')}_CLIENT_SECRET"], "standardFlowEnabled": False, "directAccessGrantsEnabled": False, "serviceAccountsEnabled": True, "protocolMappers": service_mappers} for c in AI_CLIENTS]
    realm = {"realm": "learning", "enabled": True, "sslRequired": "none", "registrationAllowed": False, "roles": {"realm": [{"name": r} for r in ["teacher", "student", "ai-client"]]}, "users": users,
             "clients": [{"clientId": "learning-web", "publicClient": True, "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "redirectUris": ["http://localhost:4200/*"], "webOrigins": ["http://localhost:4200"], "attributes": {"pkce.code.challenge.method": "S256"}, "protocolMappers": mappers}, {"clientId": "learning-e2e", "publicClient": True, "standardFlowEnabled": False, "directAccessGrantsEnabled": True, "protocolMappers": mappers}, *service_clients]}
    (local / "learning-realm.json").write_text(json.dumps(realm, indent=2), encoding="utf-8")
    # Provider keys are the developer's own: left empty here, and ai-core answers 503 until they are set.
    providers = "\n# AI provider keys, used only by ai-core (ADR 0002). Fill in by hand.\nOPENAI_API_KEY=\nVOYAGE_API_KEY=\n"
    (ROOT / ".env").write_text("# Generated local development secrets. Never commit.\n" + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n" + providers, encoding="utf-8")
    print("Created .env and .local infrastructure configuration. Local user passwords are in .env.")


if __name__ == "__main__":
    main()
