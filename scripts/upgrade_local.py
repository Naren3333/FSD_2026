"""Bring a local setup made by an older scripts/setup.py up to date, without touching existing data or secrets.

Adds missing secrets to .env (it never changes an existing line), creates missing service databases and roles in
the running Compose PostgreSQL (for example `chatbot`, added in ADR 0002 stage 4, and `aicore`, ai-core's usage
ledger from ADR 0003), and creates missing AI service-account clients in the running Keycloak (for example
`grading-marker`, ADR 0004) and Identity's `identity-accounts` client (ADR 0011). Existing clients, users and
databases are left as they are, but every service account still gets its role, so a run that stopped half-way is
finished by the next, and the existing `learning-web` client gets its sign-out redirect setting and loses the test
stack's web origin, which older realm imports listed. Safe to run again.
It also rewrites the generated realm import from .env, for the next fresh Keycloak volume. Other realm changes to
a running Keycloak still need a fresh volume (see operations.md).
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_stack import KEYCLOAK_PORT, ROOT, TEST_PORTS, admin_client, check, psql, psql_value, read_env
from setup import (
    ACCOUNTS_CLIENT,
    ACCOUNTS_ROLE,
    AI_CLIENTS,
    PEOPLE,
    SERVICES,
    TEST_WEB_ORIGIN,
    WEB_ATTRIBUTES,
    accounts_client,
    client_secret_key,
    generate_secrets,
    password_key,
    realm,
    service_client,
)

if TYPE_CHECKING:
    import httpx

# Values only read when a volume is first created: added to an .env that lacks them, they do not change a running
# PostgreSQL superuser, Keycloak admin or local person, whose existing passwords stay in effect.
FIRST_START_ONLY = {"POSTGRES_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", *(key for _, _, key in PEOPLE)}


def add_missing_secrets(env_path: Path) -> dict[str, str]:
    """Appends every value scripts/setup.py generates that .env lacks (a database password per service, a client
    secret per AI client, and so on) and returns every value in .env. Existing lines are never changed."""
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
    for name in SERVICES:
        if psql_value(f"SELECT count(*) FROM pg_roles WHERE rolname='{name}'") == "0":
            psql_value(f"CREATE ROLE {name} LOGIN PASSWORD '{env[password_key(name)]}'")
            print(f"Created role {name}")
        if psql_value(f"SELECT count(*) FROM pg_database WHERE datname='{name}'") == "0":
            psql_value(f"CREATE DATABASE {name} OWNER {name}")
            psql_value(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
            print(f"Created database {name}")


def add_missing_clients(env: dict[str, str]) -> None:
    """Creates each AI client Keycloak lacks, with .env's secret, and gives its service account the ai-client
    role. The role is assigned to existing clients too (Keycloak ignores a mapping it already has), so a run that
    stopped after creating a client is finished by the next one."""
    with admin_client() as admin:
        for client in AI_CLIENTS:
            created = ensure_client(
                admin, client, lambda c=client: service_client(c, env[client_secret_key(c)])
            )
            client_id = check(admin.get("/clients", params={"clientId": client})).json()[0]["id"]
            account = check(admin.get(f"/clients/{client_id}/service-account-user")).json()
            role = check(admin.get("/roles/ai-client")).json()
            check(admin.post(f"/users/{account['id']}/role-mappings/realm", json=[role]))
            if created:
                print(f"Created Keycloak client {client}")


def add_missing_accounts_client(env: dict[str, str]) -> None:
    """Creates Identity's identity-accounts client if Keycloak lacks it, with .env's secret, and gives its service
    account realm-management's manage-users role. The role is assigned to an existing client too, so a run that
    stopped after creating the client is finished by the next one; without it Identity cannot remove sign-ins."""
    with admin_client() as admin:
        created = ensure_client(
            admin, ACCOUNTS_CLIENT, lambda: accounts_client(env[client_secret_key(ACCOUNTS_CLIENT)])
        )
        client_id = check(admin.get("/clients", params={"clientId": ACCOUNTS_CLIENT})).json()[0]["id"]
        account = check(admin.get(f"/clients/{client_id}/service-account-user")).json()
        owner, role_name = ACCOUNTS_ROLE
        management = check(admin.get("/clients", params={"clientId": owner})).json()[0]["id"]
        role = check(admin.get(f"/clients/{management}/roles/{role_name}")).json()
        check(admin.post(f"/users/{account['id']}/role-mappings/clients/{management}", json=[role]))
        if created:
            print(f"Created Keycloak client {ACCOUNTS_CLIENT}")


def update_web_client() -> None:
    """Gives the existing learning-web client the attributes setup.py now sets (such as where Sign out may
    return to) and, in the development stack, drops the test stack's web origin that older realm imports added; the
    test stack has its own realm. Changes nothing when the client already matches."""
    with admin_client() as admin:
        found = check(admin.get("/clients", params={"clientId": "learning-web"})).json()
        if not found:
            return
        client = found[0]
        changed = updated_web_client(client, development=TEST_PORTS["KEYCLOAK_PORT"] != KEYCLOAK_PORT)
        if changed != client:
            check(admin.put(f"/clients/{client['id']}", json=changed))
            print("Updated Keycloak client learning-web")


def updated_web_client(client: dict[str, Any], *, development: bool) -> dict[str, Any]:
    """The learning-web client with setup.py's attributes and, for the development stack, without the test stack's
    web origin. Other settings are kept as they are."""
    updated = {**client, "attributes": {**(client.get("attributes") or {}), **WEB_ATTRIBUTES}}
    if development:
        test_uri = f"{TEST_WEB_ORIGIN}/*"
        updated["redirectUris"] = [uri for uri in client.get("redirectUris") or [] if uri != test_uri]
        updated["webOrigins"] = [
            origin for origin in client.get("webOrigins") or [] if origin != TEST_WEB_ORIGIN
        ]
    return updated


def ensure_client(admin: "httpx.Client", client: str, representation: Callable[[], dict[str, Any]]) -> bool:
    """Creates the client unless Keycloak has it; True when it was created."""
    if check(admin.get("/clients", params={"clientId": client})).json():
        return False
    check(admin.post("/clients", json=representation()))
    return True


def refresh_realm_import(env: dict[str, str]) -> None:
    """Rewrites the generated realm import from .env, so a fresh Keycloak volume gets the current clients too."""
    path = ROOT / ".local" / "learning-realm.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(realm(env), indent=2), encoding="utf-8")


def main() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise SystemExit("No .env: run scripts/setup.py for a new setup.")
    env = add_missing_secrets(env_path)
    refresh_realm_import(env)
    # Compose refuses to start while .env lacks a secret, so the secrets come first and the stack may be down.
    if psql("SELECT 1", check=False).returncode != 0:
        raise SystemExit(
            "Secrets are in .env, but the stack is not running. Start it with `docker compose up -d --build`, "
            "wait for `python scripts/wait_ready.py`, then run this script again."
        )
    add_missing_databases(env)
    add_missing_clients(env)
    add_missing_accounts_client(env)
    update_web_client()
    print("Local setup is up to date. Run: docker compose up -d --build")


if __name__ == "__main__":
    main()
