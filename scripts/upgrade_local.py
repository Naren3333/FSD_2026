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
    return subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()


def main() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise SystemExit("No .env: run scripts/setup.py for a new setup.")
    text = env_path.read_text(encoding="utf-8")
    env = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    added = {f"{name.upper()}_DB_PASSWORD": secrets.token_hex(20) for name in SERVICES if f"{name.upper()}_DB_PASSWORD" not in env}
    if added:
        env_path.write_text(text.rstrip("\n") + "\n" + "".join(f"{k}={v}\n" for k, v in added.items()), encoding="utf-8")
        env.update(added)
        print("Added to .env: " + ", ".join(added))
    for name in SERVICES:
        if psql(f"SELECT count(*) FROM pg_roles WHERE rolname='{name}'") == "0":
            psql(f"CREATE ROLE {name} LOGIN PASSWORD '{env[f'{name.upper()}_DB_PASSWORD']}'")
            print(f"Created role {name}")
        if psql(f"SELECT count(*) FROM pg_database WHERE datname='{name}'") == "0":
            psql(f"CREATE DATABASE {name} OWNER {name}")
            psql(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
            print(f"Created database {name}")
    print("Local setup is up to date. Run: docker compose up -d --build")


if __name__ == "__main__":
    main()
