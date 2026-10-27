"""share local sign-in, corpus metadata and private eval state."""

import base64
import importlib
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
# 429s that waiting a few seconds won't fix: the allowance is used up.
SPENT = {"AI_SPEND_LIMIT", "AI_TOTAL_SPEND_LIMIT"}
CORPUS = Path(__file__).with_name("corpus.yaml")
GOLDEN = Path(__file__).with_name("golden.yaml")
PDF_DIR = Path(os.getenv("EVAL_PDF_DIR", str(ROOT.parent / "fsd_test" / "data" / "test_pdfs")))
STATE = ROOT / ".local" / "eval-corpus.json"
RUNS = ROOT / ".local" / "eval-runs"
CLASSES = ("C1", "C2")
STUDENT2 = "eval-student2"
# the golden set's users: which account, and which of the eval classrooms it belongs to.
USERS = {"t1": ("teacher", CLASSES), "s1": ("student", ("C1",)), "s2": (STUDENT2, ("C2",))}


def service_modules(service: str, *names: str) -> list:
    """a service's own modules, imported apart from another service's modules of the same name (every agent has a prompts.py)."""
    folder = ROOT / "services" / service
    if (platform := str(ROOT / "packages" / "python-platform")) not in sys.path:
        sys.path.append(platform)  # the agents' shared package, which check.py puts on PYTHONPATH
    own = {p.stem for p in folder.glob("*.py")}
    saved = {n: sys.modules.pop(n) for n in own if n in sys.modules}
    sys.path.insert(0, str(folder))
    try:
        return [importlib.import_module(n) for n in names]
    finally:
        sys.path.remove(str(folder))
        for n in own:
            sys.modules.pop(n, None)
        sys.modules.update(saved)


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
    if user in (seeded := state.get("performance", {}).get("passwords", {})):
        return str(seeded[user])
    return env()[{"teacher": "TEACHER_PASSWORD", "student": "STUDENT_PASSWORD"}[user]]


def spent(res: httpx.Response) -> bool:
    """whether a 429 says the allowance is used up (streamed bodies are read first)."""
    try:
        res.read()
        return res.json().get("error", {}).get("code") in SPENT
    except (ValueError, AttributeError, httpx.HTTPError):
        return False


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
            elif res.status_code == 429 and not last and not spent(res):
                signed_in = False
                time.sleep(5)
            else:
                return res
            res.close()
        raise AssertionError("unreachable")

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self.send(method, path, **kwargs)

    def claims(self) -> dict:
        """the signed-in token's claims, unverified: the subject and org ai-core bills."""
        payload = self.http.headers["Authorization"].removeprefix("Bearer ").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))

    @contextmanager
    def stream(self, method: str, path: str, **kwargs) -> Iterator[httpx.Response]:
        """like request, with the response streamed inside the block."""
        res = self.send(method, path, stream=True, **kwargs)
        try:
            yield res
        finally:
            res.close()
