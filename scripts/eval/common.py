"""share local sign-in, corpus metadata and private eval state."""

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/, for local_stack
from local_stack import BASE, ROOT, env, password_token

ATTEMPTS = 30  # a 429 waits and retries this often
CORPUS = Path(__file__).with_name("corpus.yaml")
GOLDEN = Path(__file__).with_name("golden.yaml")
PDF_DIR = Path(os.getenv("EVAL_PDF_DIR", str(ROOT.parent / "fsd_test" / "data" / "test_pdfs")))
STATE = ROOT / ".local" / "eval-corpus.json"
RUNS = ROOT / ".local" / "eval-runs"
CLASSES = ("C1", "C2")
STUDENT2 = "eval-student2"
# the golden set's users: which account, and which of the eval classrooms it belongs to.
USERS = {"t1": ("teacher", CLASSES), "s1": ("student", ("C1",)), "s2": (STUDENT2, ("C2",))}


def corpus() -> dict[str, dict]:
    """file name → title, doc_type and class_ids (one class per document)."""
    return yaml.safe_load(CORPUS.read_text(encoding="utf-8"))


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def password(user: str, state: dict) -> str:
    if user == STUDENT2:
        return state["student2_password"]
    return env()[{"teacher": "TEACHER_PASSWORD", "student": "STUDENT_PASSWORD"}[user]]


class Session:
    """gateway calls as a local test user (the learning-e2e client, as scripts/integration.py does)."""

    def __init__(self, user: str, state: dict) -> None:
        self.user, self.secret = user, password(user, state)
        self.http = httpx.Client(base_url=BASE, timeout=60)
        self.sign_in()

    def sign_in(self) -> None:
        self.http.headers["Authorization"] = "Bearer " + password_token(self.user, self.secret)

    def send(self, method: str, path: str, *, stream: bool = False, **kwargs) -> httpx.Response:
        """the response, after signing in again on a 401 and waiting out 429s."""
        signed_in = False
        for attempt in range(ATTEMPTS):
            res = self.http.send(self.http.build_request(method, path, **kwargs), stream=stream)
            last = attempt == ATTEMPTS - 1
            if res.status_code == 401 and not signed_in and not last:
                self.sign_in()
                signed_in = True
            elif res.status_code == 429 and not last:
                signed_in = False
                time.sleep(5)
            else:
                return res
            res.close()
        raise AssertionError("unreachable")

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self.send(method, path, **kwargs)

    @contextmanager
    def stream(self, method: str, path: str, **kwargs) -> Iterator[httpx.Response]:
        """like request, with the response streamed inside the block."""
        res = self.send(method, path, stream=True, **kwargs)
        try:
            yield res
        finally:
            res.close()
