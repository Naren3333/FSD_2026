"""Generate local-only secrets and Keycloak realm. Never overwrites an existing setup."""
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["identity", "curriculum", "content", "assessment", "grading", "analytics"]


def main() -> None:
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    if (ROOT / ".env").exists():
        raise SystemExit(".env already exists; preserving existing credentials. See README for recovery.")
    values = {key: secrets.token_hex(20) for key in ["POSTGRES_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", "S3_ROOT_PASSWORD", "TEACHER_PASSWORD", "STUDENT_PASSWORD", "OTHER_TEACHER_PASSWORD", "S3_CONTENT_PASSWORD", *[f"{s.upper()}_DB_PASSWORD" for s in SERVICES]]}
    values["S3_ROOT_USER"] = "local-admin"
    values["S3_CONTENT_USER"] = "content-service"
    sql = []
    for name in SERVICES:
        password = values[f"{name.upper()}_DB_PASSWORD"]
        sql += [f"CREATE ROLE {name} LOGIN PASSWORD '{password}';", f"CREATE DATABASE {name} OWNER {name};", f"REVOKE ALL ON DATABASE {name} FROM PUBLIC;"]
    (local / "postgres-init.sql").write_text("\n".join(sql), encoding="utf-8")
    users = []
    for username, role, key in [("teacher", "teacher", "TEACHER_PASSWORD"), ("student", "student", "STUDENT_PASSWORD"), ("other-teacher", "teacher", "OTHER_TEACHER_PASSWORD")]:
        users.append({"username": username, "enabled": True, "emailVerified": True, "firstName": username.title(), "lastName": "Local", "email": f"{username}@example.test", "attributes": {"org_id": ["local-school"]}, "realmRoles": [role], "credentials": [{"type": "password", "value": values[key], "temporary": False}]})
    mappers = [{"name": "learning-audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper", "config": {"included.custom.audience": "learning", "access.token.claim": "true"}}, {"name": "organization", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-attribute-mapper", "config": {"user.attribute": "org_id", "claim.name": "org_id", "jsonType.label": "String", "access.token.claim": "true", "id.token.claim": "true"}}]
    realm = {"realm": "learning", "enabled": True, "sslRequired": "none", "registrationAllowed": False, "roles": {"realm": [{"name": r} for r in ["teacher", "student", "ai-client"]]}, "users": users,
             "clients": [{"clientId": "learning-web", "publicClient": True, "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "redirectUris": ["http://localhost:4200/*"], "webOrigins": ["http://localhost:4200"], "attributes": {"pkce.code.challenge.method": "S256"}, "protocolMappers": mappers}, {"clientId": "learning-e2e", "publicClient": True, "standardFlowEnabled": False, "directAccessGrantsEnabled": True, "protocolMappers": mappers}]}
    (local / "learning-realm.json").write_text(json.dumps(realm, indent=2), encoding="utf-8")
    (ROOT / ".env").write_text("# Generated local development secrets. Never commit.\n" + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    print("Created .env and .local infrastructure configuration. Local user passwords are in .env.")


if __name__ == "__main__":
    main()
