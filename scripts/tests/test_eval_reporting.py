"""paid evals must support bounded selections and retain usable partial results."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from reporting import checkpoint, select_cases


def test_selection_rejects_unknown_ids_and_preserves_definition_order():
    cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert select_cases(cases, ["c", "a", "a"]) == [cases[0], cases[2]]
    with pytest.raises(ValueError, match="excluded"):
        select_cases(cases, ["typo"])


def test_checkpoint_keeps_last_report_if_replacement_fails(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    checkpoint(path, {"status": "running", "cases": [{"id": "completed"}]})
    previous = json.loads(path.read_text())

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        checkpoint(path, {"status": "complete", "cases": []})
    assert json.loads(path.read_text()) == previous


@pytest.mark.parametrize("fail_second", [False, True])
def test_chat_runner_reports_progress_and_preserves_completed_cases(
    tmp_path, monkeypatch, capsys, fail_second
):
    import run_chat

    cases = [
        {"id": name, "as": "s1", "group": "grounded", "turns": ["question"]} for name in ["first", "second"]
    ]
    monkeypatch.setattr(sys, "argv", ["run_chat.py"])
    monkeypatch.setattr(run_chat, "RUNS", tmp_path)
    monkeypatch.setattr(run_chat, "load_state", lambda: {"documents": {}})
    monkeypatch.setattr(run_chat, "load_cases", lambda *_: cases)
    monkeypatch.setattr(run_chat, "chunk_map", lambda *_: {})
    monkeypatch.setattr(run_chat, "assess", lambda *_: None)

    def run(case, state):
        if fail_second and case["id"] == "second":
            raise RuntimeError("connection lost")
        return case | {"results": [], "failures": [], "flags": []}

    monkeypatch.setattr(run_chat, "run_case", run)
    if fail_second:
        with pytest.raises(RuntimeError, match="connection lost"):
            run_chat.main()
    else:
        with pytest.raises(SystemExit) as exit_info:
            run_chat.main()
        assert exit_info.value.code == 0
    [report_path] = list(tmp_path.glob("chat-*.json"))
    report = json.loads(report_path.read_text())
    assert report["status"] == ("interrupted" if fail_second else "complete")
    assert [c["id"] for c in report["cases"]] == (["first"] if fail_second else ["first", "second"])
    assert "[1/2] ok: first" in capsys.readouterr().out
