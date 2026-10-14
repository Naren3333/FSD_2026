"""Shared by the golden eval scripts: local sign-in through Keycloak, the corpus manifest, and the eval's state in
.local/ (classroom and document ids, the second student's generated password). Local development stack only."""

import json
import os
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if line and not line.startswith("#")
)
BASE = "http://localhost:8000/api"
KEYCLOAK = "http://localhost:8081"
CORPUS = Path(__file__).with_name("corpus.yaml")
GOLDEN = Path(__file__).with_name("golden.yaml")
PDF_DIR = Path(os.getenv("EVAL_PDF_DIR", str(ROOT.parent / "fsd_test" / "data" / "test_pdfs")))
STATE = ROOT / ".local" / "eval-corpus.json"
RUNS = ROOT / ".local" / "eval-runs"
CLASSES = ("C1", "C2")
STUDENT2 = "eval-student2"
# The golden set's users: which account, and which of the eval classrooms it belongs to.
USERS = {"t1": ("teacher", CLASSES), "s1": ("student", ("C1",)), "s2": (STUDENT2, ("C2",))}


def corpus() -> dict[str, dict]:
    """File name → title, doc_type and class_ids (one class per document)."""
    return yaml.safe_load(CORPUS.read_text(encoding="utf-8"))


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def password(user: str, state: dict) -> str:
    if user == STUDENT2:
        return state["student2_password"]
    return ENV[{"teacher": "TEACHER_PASSWORD", "student": "STUDENT_PASSWORD"}[user]]


class Session:
    """Gateway calls as a local test user (the learning-e2e client, as scripts/integration.py does). Access tokens
    are short-lived, so a 401 signs in again once. Kong allows 120 requests a minute for everyone, so a 429
    waits and retries."""

    def __init__(self, user: str, state: dict) -> None:
        self.user, self.secret = user, password(user, state)
        self.http = httpx.Client(base_url=BASE, timeout=60)
        self.sign_in()

    def sign_in(self) -> None:
        res = httpx.post(
            f"{KEYCLOAK}/realms/learning/protocol/openid-connect/token",
            timeout=15,
            data={
                "client_id": "learning-e2e",
                "grant_type": "password",
                "username": self.user,
                "password": self.secret,
            },
        )
        if res.status_code != 200:
            raise SystemExit(f"Local sign-in failed for {self.user}: {res.status_code}")
        self.http.headers["Authorization"] = "Bearer " + res.json()["access_token"]

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(30):
            res = self.http.request(method, path, **kwargs)
            if res.status_code == 401 and attempt == 0:
                self.sign_in()
            elif res.status_code == 429:
                time.sleep(5)
            else:
                break
        return res


def check(res: httpx.Response) -> httpx.Response:
    if res.is_error:
        raise SystemExit(
            f"{res.request.method} {res.request.url.path} -> {res.status_code}: {res.text[:300]}"
        )
    return res
