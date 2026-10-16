"""Bring a local setup made by an older scripts/setup.py up to date, without touching existing data or secrets.

Adds missing secrets to .env (it never changes an existing line), creates missing service databases and roles in
the running Compose PostgreSQL (for example `chatbot`, added in ADR 0002 stage 4, and `aicore`, ai-core's usage
ledger from ADR 0003), and creates missing AI service-account clients in the running Keycloak (for example
`grading-marker`, ADR 0004). Existing clients, users and databases are left as they are. Safe to run again.
It also rewrites the generated realm import from .env, for the next fresh Keycloak volume. Other realm changes to
a running Keycloak still need a fresh volume (see operations.md).
"""

import json
import secrets
from pathlib import Path

from local_stack import ROOT, admin_client, check, psql_value, read_env
from setup import AI_CLIENTS, SERVICES, client_secret_key, password_key, realm, service_client


def add_missing_secrets(env_path: Path) -> dict[str, str]:
    """Appends a database password for each service and a client secret for each AI client .env lacks; returns
    every value in .env."""
    text = env_path.read_text(encoding="utf-8")
    env = read_env(env_path)
    keys = [password_key(name) for name in SERVICES] + [client_secret_key(c) for c in AI_CLIENTS]
    added = {key: secrets.token_hex(20) for key in keys if not env.get(key)}
    if added:
        lines = "".join(f"{k}={v}\n" for k, v in added.items())
        env_path.write_text(text.rstrip("\n") + "\n" + lines, encoding="utf-8")
        env.update(added)
        print("Added to .env: " + ", ".join(added))
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
    role."""
    with admin_client() as admin:
        for client in AI_CLIENTS:
            if check(admin.get("/clients", params={"clientId": client})).json():
                continue
            check(admin.post("/clients", json=service_client(client, env[client_secret_key(client)])))
            client_id = check(admin.get("/clients", params={"clientId": client})).json()[0]["id"]
            account = check(admin.get(f"/clients/{client_id}/service-account-user")).json()
            role = check(admin.get("/roles/ai-client")).json()
            check(admin.post(f"/users/{account['id']}/role-mappings/realm", json=[role]))
            print(f"Created Keycloak client {client}")


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
    add_missing_databases(env)
    add_missing_clients(env)
    refresh_realm_import(env)
    print("Local setup is up to date. Run: docker compose up -d --build")


if __name__ == "__main__":
    main()
