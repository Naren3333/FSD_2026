"""Bring a local setup made by an older scripts/setup.py up to date, without touching existing data or secrets.

Adds missing secrets to .env (it never changes an existing line), and creates missing service databases and
roles in the running Compose PostgreSQL (for example `chatbot`, added in ADR 0002 stage 4, and `aicore`, ai-core's
usage ledger from ADR 0003). Safe to run again.
Keycloak clients are not added here: a realm change still needs a fresh Keycloak volume (see operations.md).
"""

import secrets
import subprocess
from pathlib import Path

from setup import SERVICES

ROOT = Path(__file__).resolve().parents[1]


def psql(sql: str) -> str:
    """Runs one statement as the PostgreSQL superuser, inside the Compose container."""
    command = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres"]
    command += ["-v", "ON_ERROR_STOP=1", "-At", "-c", sql]
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()


def password_key(service: str) -> str:
    return f"{service.upper()}_DB_PASSWORD"


def add_missing_secrets(env_path: Path) -> dict[str, str]:
    """Appends a password for each service .env lacks; returns every value in .env."""
    text = env_path.read_text(encoding="utf-8")
    env = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    added = {password_key(name): secrets.token_hex(20) for name in SERVICES if password_key(name) not in env}
    if added:
        lines = "".join(f"{k}={v}\n" for k, v in added.items())
        env_path.write_text(text.rstrip("\n") + "\n" + lines, encoding="utf-8")
        env.update(added)
        print("Added to .env: " + ", ".join(added))
    return env


def add_missing_databases(env: dict[str, str]) -> None:
    for name in SERVICES:
        if psql(f"SELECT count(*) FROM pg_roles WHERE rolname='{name}'") == "0":
            psql(f"CREATE ROLE {name} LOGIN PASSWORD '{env[password_key(name)]}'")
            print(f"Created role {name}")
        if psql(f"SELECT count(*) FROM pg_database WHERE datname='{name}'") == "0":
            psql(f"CREATE DATABASE {name} OWNER {name}")
            psql(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
            print(f"Created database {name}")


def main() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise SystemExit("No .env: run scripts/setup.py for a new setup.")
    add_missing_databases(add_missing_secrets(env_path))
    print("Local setup is up to date. Run: docker compose up -d --build")


if __name__ == "__main__":
    main()
