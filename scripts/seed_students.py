"""Add thirty test students to the local stack so a teacher can enroll them.

    python scripts/seed_students.py

Creates each student in the local Keycloak realm (role student, organization local-school) and signs them in
once, which registers them with Identity; until then a teacher cannot enroll them by username. All thirty share
one generated password, kept in .local/seed-students.json (gitignored) and printed at the end. Safe to re-run:
existing students are kept and given the saved password. Local development stack only; requires .env and httpx.
"""

import json
import secrets

import httpx
from local_stack import BASE, ROOT, admin_client, check, ensure_person, password_token
from setup import ORG

STATE = ROOT / ".local" / "seed-students.json"
STUDENTS = [
    ("Aisha", "Rahman"),
    ("Ben", "Tan"),
    ("Chloe", "Lim"),
    ("Daniel", "Wong"),
    ("Emily", "Ng"),
    ("Farhan", "Ismail"),
    ("Grace", "Koh"),
    ("Hari", "Kumar"),
    ("Isabel", "Chua"),
    ("Jun", "Ong"),
    ("Kavya", "Nair"),
    ("Liam", "Goh"),
    ("Mei", "Ling"),
    ("Nadia", "Yusof"),
    ("Oliver", "Teo"),
    ("Priya", "Menon"),
    ("Qi", "Hao"),
    ("Rachel", "Seah"),
    ("Samuel", "Low"),
    ("Tara", "Pillai"),
    ("Umar", "Hassan"),
    ("Vanessa", "Yeo"),
    ("Wei", "Jie"),
    ("Xin", "Yi"),
    ("Yusuf", "Ali"),
    ("Zara", "Quek"),
    ("Arjun", "Das"),
    ("Bella", "Foo"),
    ("Caleb", "Sim"),
    ("Diana", "Heng"),
]


def register(username: str, password: str) -> None:
    """Signs the student in once, which registers them with Identity."""
    token = password_token(username, password)
    profile = check(
        httpx.post(f"{BASE}/identity/me", json={}, headers={"Authorization": "Bearer " + token}, timeout=15)
    ).json()
    if profile["role"] != "student" or profile["org_id"] != ORG:
        raise SystemExit(f"{username} did not register as a {ORG} student: {profile}")


def main() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    password = state.setdefault("password", secrets.token_urlsafe(12))
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    for first, last in STUDENTS:
        username = f"{first}.{last}".lower()
        # Keycloak's admin tokens last a minute, so each student gets a fresh one.
        with admin_client() as admin:
            ensure_person(admin, username, first, last, password)
        register(username, password)
        print(f"{username:18} {first} {last}")
    print(f"\nPassword for all thirty: {password}  (saved in {STATE.relative_to(ROOT)})")


if __name__ == "__main__":
    main()
