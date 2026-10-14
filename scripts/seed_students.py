"""Add ten test students to the local stack so a teacher can enroll them.

    python scripts/seed_students.py

Creates each student in the local Keycloak realm (role student, organization local-school) and signs them in
once, which registers them with Identity; until then a teacher cannot enroll them by username. All ten share
one generated password, kept in .local/seed-students.json (gitignored) and printed at the end. Safe to re-run:
existing students are kept and given the saved password. Local development stack only; requires .env and httpx.
"""

import json
import secrets
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if line and not line.startswith("#")
)
KEYCLOAK = "http://localhost:8081"
BASE = "http://localhost:8000/api"
ORG = "local-school"
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
]


def check(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        raise SystemExit(
            f"{response.request.method} {response.request.url}: {response.status_code} {response.text}"
        )
    return response


def admin_client() -> httpx.Client:
    data = {
        "client_id": "admin-cli",
        "grant_type": "password",
        "username": "admin",
        "password": ENV["KEYCLOAK_ADMIN_PASSWORD"],
    }
    token = check(
        httpx.post(f"{KEYCLOAK}/realms/master/protocol/openid-connect/token", data=data, timeout=15)
    )
    return httpx.Client(
        base_url=f"{KEYCLOAK}/admin/realms/learning",
        headers={"Authorization": "Bearer " + token.json()["access_token"]},
        timeout=15,
    )


def ensure_student(admin: httpx.Client, username: str, first: str, last: str, password: str) -> None:
    """Creates the student, or resets an existing one's password, then gives them the student role and the
    organization. org_id is an unmanaged attribute that the admin API only keeps while the realm lets admins
    edit those, so that is allowed just long enough to set it."""
    found = check(admin.get("/users", params={"username": username, "exact": "true"})).json()
    credential = {"type": "password", "value": password, "temporary": False}
    if found:
        user_id = found[0]["id"]
        check(admin.put(f"/users/{user_id}/reset-password", json=credential))
    else:
        user = {
            "username": username,
            "enabled": True,
            "emailVerified": True,
            "firstName": first,
            "lastName": last,
            "email": f"{username}@example.test",
            "credentials": [credential],
        }
        check(admin.post("/users", json=user))
        user_id = check(admin.get("/users", params={"username": username, "exact": "true"})).json()[0]["id"]
    role = check(admin.get("/roles/student")).json()
    check(admin.post(f"/users/{user_id}/role-mappings/realm", json=[role]))
    profile = check(admin.get("/users/profile")).json()
    check(admin.put("/users/profile", json={**profile, "unmanagedAttributePolicy": "ADMIN_EDIT"}))
    try:
        user = check(admin.get(f"/users/{user_id}")).json()
        attributes = {**(user.get("attributes") or {}), "org_id": [ORG]}
        check(admin.put(f"/users/{user_id}", json={**user, "attributes": attributes}))
    finally:
        check(admin.put("/users/profile", json=profile))


def register(username: str, password: str) -> None:
    """Signs the student in once, which registers them with Identity."""
    data = {"client_id": "learning-e2e", "grant_type": "password", "username": username, "password": password}
    token = check(
        httpx.post(f"{KEYCLOAK}/realms/learning/protocol/openid-connect/token", data=data, timeout=15)
    ).json()["access_token"]
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
    admin = admin_client()
    for first, last in STUDENTS:
        username = f"{first}.{last}".lower()
        ensure_student(admin, username, first, last, password)
        register(username, password)
        print(f"{username:18} {first} {last}")
    print(f"\nPassword for all ten: {password}  (saved in {STATE.relative_to(ROOT)})")


if __name__ == "__main__":
    main()
