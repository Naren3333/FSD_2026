"""bring a local setup made by an older scripts/setup.py up to date, without touching existing data or secrets."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_stack import (
    KEYCLOAK_PORT,
    ROOT,
    TEST_PORTS,
    admin_client,
    apply_user_profile,
    check,
    ensure_person,
    psql,
    psql_value,
    read_env,
)
from setup import (
    ACCOUNTS_CLIENT,
    ACCOUNTS_ROLES,
    ADMIN_ROLE,
    AI_CLIENTS,
    DATABASES,
    LEGACY_WEB_ORIGINS,
    MONITORING_CLIENT,
    PEOPLE,
    PERSON_ROLES,
    TEST_WEB_ORIGIN,
    WEB_ATTRIBUTES,
    WEB_ORIGIN,
    accounts_client,
    client_secret_key,
    generate_secrets,
    password_key,
    realm,
    service_client,
)

if TYPE_CHECKING:
    import httpx

# new environment defaults affect fresh volumes only; existing account passwords stay in effect. the admin
# account is created by this script when missing (add_missing_admin), so its password always applies.
FIRST_START_ONLY = {
    "POSTGRES_PASSWORD",
    "KEYCLOAK_ADMIN_PASSWORD",
    *(key for _, role, key in PEOPLE if role != ADMIN_ROLE),
}


def add_missing_secrets(env_path: Path) -> dict[str, str]:
    """appends every value scripts/setup.py generates that .env lacks (a database password per service, a client secret per ai client, and so on) and returns every value in .env."""
    text = env_path.read_text(encoding="utf-8")
    env = read_env(env_path)
    added = {key: value for key, value in generate_secrets().items() if not env.get(key)}
    if added:
        lines = "".join(f"{k}={v}\n" for k, v in added.items())
        env_path.write_text(text.rstrip("\n") + "\n" + lines, encoding="utf-8")
        env.update(added)
        print("Added to .env: " + ", ".join(added))
        stale = sorted(FIRST_START_ONLY & added.keys())
        if stale:
            print(
                "Note: " + ", ".join(stale) + " only take effect on fresh volumes; existing ones keep their "
                "passwords (see operations.md)."
            )
    return env


def add_missing_databases(env: dict[str, str]) -> None:
    for name in DATABASES:
        if psql_value(f"SELECT count(*) FROM pg_roles WHERE rolname='{name}'") == "0":
            psql_value(f"CREATE ROLE {name} LOGIN PASSWORD '{env[password_key(name)]}'")
            print(f"Created role {name}")
        if psql_value(f"SELECT count(*) FROM pg_database WHERE datname='{name}'") == "0":
            psql_value(f"CREATE DATABASE {name} OWNER {name}")
            psql_value(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
            print(f"Created database {name}")


def add_missing_clients(env: dict[str, str]) -> None:
    """provisions client credentials with their roles, without assigning any person role."""
    with admin_client() as admin:
        for name in (*PERSON_ROLES, MONITORING_CLIENT):
            if admin.get(f"/roles/{name}").status_code == 404:
                check(admin.post("/roles", json={"name": name}))
        for client in [*AI_CLIENTS, MONITORING_CLIENT]:
            created = ensure_client(
                admin, client, lambda c=client: service_client(c, env[client_secret_key(c)])
            )
            client_id = check(admin.get("/clients", params={"clientId": client})).json()[0]["id"]
            account = check(admin.get(f"/clients/{client_id}/service-account-user")).json()
            role_name = MONITORING_CLIENT if client == MONITORING_CLIENT else "ai-client"
            role = check(admin.get(f"/roles/{role_name}")).json()
            check(admin.post(f"/users/{account['id']}/role-mappings/realm", json=[role]))
            if created:
                print(f"Created Keycloak client {client}")


def add_missing_accounts_client(env: dict[str, str]) -> None:
    """creates identity's identity-accounts client if keycloak lacks it, with .env's secret, and gives its service account realm-management's manage-users and view-realm roles."""
    with admin_client() as admin:
        created = ensure_client(
            admin, ACCOUNTS_CLIENT, lambda: accounts_client(env[client_secret_key(ACCOUNTS_CLIENT)])
        )
        client_id = check(admin.get("/clients", params={"clientId": ACCOUNTS_CLIENT})).json()[0]["id"]
        account = check(admin.get(f"/clients/{client_id}/service-account-user")).json()
        owner, role_names = ACCOUNTS_ROLES
        management = check(admin.get("/clients", params={"clientId": owner})).json()[0]["id"]
        roles = [check(admin.get(f"/clients/{management}/roles/{name}")).json() for name in role_names]
        check(admin.post(f"/users/{account['id']}/role-mappings/clients/{management}", json=roles))
        if created:
            print(f"Created Keycloak client {ACCOUNTS_CLIENT}")


def add_missing_admin(env: dict[str, str]) -> None:
    """lets identity give new accounts their school (org_id) without an email, and creates the local school's admin with .env's password unless keycloak has one; an existing admin keeps its password."""
    with admin_client() as admin:
        apply_user_profile(admin)
        username, role, key = next(person for person in PEOPLE if person[1] == ADMIN_ROLE)
        found = check(admin.get("/users", params={"username": username, "exact": "true"})).json()
        if found:
            held = check(admin.get(f"/users/{found[0]['id']}/role-mappings/realm")).json()
            others = sorted({r["name"] for r in held} & (set(PERSON_ROLES) - {role}))
            if others:
                # an account holds one person role, so giving it this one too would lock it out.
                raise SystemExit(
                    f"Keycloak user {username} already holds the {', '.join(others)} role; rename it before upgrading."
                )
        ensure_person(admin, username, "School", "Admin", env[key], role=role, reset_password=False)
        if not found:
            print(f"Created the local school admin {username} (password: {key} in .env)")


def update_web_client() -> None:
    """give the browser client setup.py's attributes and this stack's web origin behind the edge, dropping the origins it no longer serves."""
    with admin_client() as admin:
        found = check(admin.get("/clients", params={"clientId": "learning-web"})).json()
        if not found:
            return
        client = found[0]
        development = TEST_PORTS["KEYCLOAK_PORT"] != KEYCLOAK_PORT
        changed = updated_web_client(client, WEB_ORIGIN if development else TEST_WEB_ORIGIN)
        if changed != client:
            check(admin.put(f"/clients/{client['id']}", json=changed))
            print("Updated Keycloak client learning-web")


def updated_web_client(client: dict[str, Any], web_origin: str) -> dict[str, Any]:
    """the learning-web client with setup.py's attributes, allowing `web_origin` and no other stack's or older origin."""
    dropped = {*LEGACY_WEB_ORIGINS, WEB_ORIGIN, TEST_WEB_ORIGIN} - {web_origin}
    redirects = [uri for uri in client.get("redirectUris") or [] if uri.removesuffix("/*") not in dropped]
    origins = [origin for origin in client.get("webOrigins") or [] if origin not in dropped]
    return {
        **client,
        "attributes": {**(client.get("attributes") or {}), **WEB_ATTRIBUTES},
        "redirectUris": redirects if f"{web_origin}/*" in redirects else [*redirects, f"{web_origin}/*"],
        "webOrigins": origins if web_origin in origins else [*origins, web_origin],
    }


def ensure_client(admin: "httpx.Client", client: str, representation: Callable[[], dict[str, Any]]) -> bool:
    """creates the client unless keycloak has it; true when it was created."""
    if check(admin.get("/clients", params={"clientId": client})).json():
        return False
    check(admin.post("/clients", json=representation()))
    return True


def refresh_realm_import(env: dict[str, str]) -> None:
    """rewrites the generated realm import from .env, so a fresh keycloak volume gets the current clients too."""
    path = ROOT / ".local" / "learning-realm.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(realm(env), indent=2), encoding="utf-8")


def main() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise SystemExit("No .env: run scripts/setup.py for a new setup.")
    env = add_missing_secrets(env_path)
    refresh_realm_import(env)
    # compose refuses to start while .env lacks a secret, so the secrets come first and the stack may be
    # down.
    if psql("SELECT 1", check=False).returncode != 0:
        raise SystemExit(
            "Secrets are in .env, but the stack is not running. Start it with `docker compose up -d --build`, "
            "wait for `python scripts/wait_ready.py`, then run this script again."
        )
    add_missing_databases(env)
    add_missing_clients(env)
    add_missing_accounts_client(env)
    add_missing_admin(env)
    update_web_client()
    # keycloak-migrate copies a development-mode realm out of keycloak's h2 file, which keycloak must not hold
    # open meanwhile (infra/keycloak/migrate.sh).
    print("Local setup is up to date. Run: docker compose stop keycloak, then docker compose up -d --build")


if __name__ == "__main__":
    main()
