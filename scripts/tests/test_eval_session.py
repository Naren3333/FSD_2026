"""the golden evals' gateway session signs in again whenever its token expires, even after waiting out 429s."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import common  # scripts/eval is not a package


def session(monkeypatch: pytest.MonkeyPatch, statuses: list[int]) -> tuple[common.Session, list[str]]:
    sign_ins: list[str] = []
    monkeypatch.setattr(common, "password", lambda *_: "pw")
    monkeypatch.setattr(
        common, "password_token", lambda *_: sign_ins.append("signed in") or f"t{len(sign_ins)}"
    )
    monkeypatch.setattr(common.time, "sleep", lambda _: None)
    answers = iter(statuses)
    made = common.Session("teacher", {})
    made.http = httpx.Client(
        base_url="http://gateway.test",
        headers=made.http.headers,
        transport=httpx.MockTransport(lambda _: httpx.Response(next(answers))),
    )
    return made, sign_ins


def test_a_401_after_429s_signs_in_again(monkeypatch: pytest.MonkeyPatch) -> None:
    made, sign_ins = session(monkeypatch, [401, 200, 429, 429, 401, 200])
    assert made.request("GET", "/a").status_code == 200
    assert made.request("GET", "/b").status_code == 200
    assert len(sign_ins) == 3  # the first sign-in, then one per expiry


def test_a_401_straight_after_signing_in_is_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    made, sign_ins = session(monkeypatch, [401, 401])
    assert made.request("GET", "/a").status_code == 401
    assert len(sign_ins) == 2
