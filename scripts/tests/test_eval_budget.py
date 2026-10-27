"""paid agent evals stop before a case could pass their cap, and refuse to start without the allowance for it."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import budget  # scripts/eval is not a package

TEACHER = "4f0b1b9e-7d0c-4a8e-9d55-0f6a1d3c2b10"


def test_estimates_start_at_the_floor_and_follow_the_dearest_case(monkeypatch):
    costs = iter([0.01, 0.08, 0.02])
    monkeypatch.setattr(budget, "ledger_cost", lambda _: next(costs))
    b = budget.Budget(1.0, TEACHER)
    assert b.estimate("performance") == budget.FLOORS["performance"]
    for _ in range(3):
        b.after("performance", budget.correlation())
    assert b.spent == pytest.approx(0.11)
    assert b.estimate("performance") == 0.08
    assert b.estimate("probe") == budget.FLOORS["probe"]


def test_a_case_that_could_pass_the_cap_is_not_started(monkeypatch):
    monkeypatch.setattr(budget, "ledger_cost", lambda _: 0.04)
    b = budget.Budget(0.1, TEACHER)
    for _ in range(2):  # 0.00 and then 0.04 spent, each with the 0.05 floor to come, fit
        b.before("generation")
        b.after("generation", budget.correlation())
    with pytest.raises(budget.CapReachedError, match=r"\$0.10 cap"):
        b.before("generation")  # 0.08 spent + 0.05 would not


def test_preflight_refuses_when_the_allowance_left_is_under_the_cap(monkeypatch, capsys):
    monkeypatch.setattr(budget, "teacher_limit", lambda: 10.0)
    monkeypatch.setattr(budget, "spent_since", lambda *_: 9.8)
    with pytest.raises(SystemExit, match="under the"):
        budget.Budget(0.5, TEACHER).preflight()
    monkeypatch.setattr(budget, "spent_since", lambda *_: 1.0)
    budget.Budget(0.5, TEACHER).preflight()
    assert "capped at $0.50" in capsys.readouterr().out


def test_the_limit_falls_back_to_the_compose_default(monkeypatch):
    def fail(*_, **__):
        raise subprocess.CalledProcessError(1, "docker")

    monkeypatch.setattr(budget.subprocess, "run", fail)
    assert budget.teacher_limit() == budget.DEFAULT_LIMIT


def test_ledger_queries_take_only_uuids(monkeypatch):
    seen = []
    monkeypatch.setattr(budget, "psql_json", lambda sql, **_: seen.append(sql) or [{"cost": 0.5, "n": 2}])
    sent = budget.correlation()
    assert budget.ledger_cost(sent) == 0.5
    assert budget.ledger_calls(sent, "generate") == 2
    assert sent in seen[0]
    with pytest.raises(ValueError):
        budget.ledger_cost("x' OR '1'='1")
