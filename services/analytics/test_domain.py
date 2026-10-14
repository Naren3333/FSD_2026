import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from domain import Evidence, SkillMetric, summarize


def test_no_fabricated_metrics():
    assert summarize([]) == []


def test_observed_accuracy_and_insufficient_evidence():
    data = [{"skill_id": "fractions", "correct": n < 6} for n in range(10)]
    assert summarize(data)[0]["observed_accuracy"] == 0.6
    assert summarize(data[:2])[0]["evidence_status"] == "insufficient_evidence"
    assert "mastery" not in summarize(data)[0]


def test_reject_inconsistent_evidence():
    with pytest.raises(ValidationError):
        Evidence(question_id=uuid4(), skill_id=uuid4(), correct=True, awarded=0, possible=1)


def test_response_contract_rejects_invented_accuracy():
    metric = summarize([{"skill_id": str(uuid4()), "correct": True}])[0]
    assert SkillMetric.model_validate(metric).observed_accuracy == 1
    with pytest.raises(ValidationError):
        SkillMetric.model_validate({**metric, "observed_accuracy": 0.9})


def test_consumer_restart_backoff():
    from learning_transport.kafka import RESTART_FIRST, RESTART_MAX, restart_wait

    assert restart_wait(None, False) == RESTART_FIRST
    assert restart_wait(RESTART_FIRST, False) == 2 * RESTART_FIRST
    assert restart_wait(RESTART_MAX, False) == RESTART_MAX
    assert restart_wait(RESTART_MAX, True) == RESTART_FIRST


def test_malformed_classroom_deletions_are_dead_lettered():
    from consumer import CLASSROOM_DELETED, handler

    room = str(uuid4())
    deletion = {
        "event_id": str(uuid4()),
        "event_type": CLASSROOM_DELETED,
        "schema_version": 1,
        "timestamp": "2026-10-14T00:00:00Z",
        "correlation_id": str(uuid4()),
        "resource_id": str(uuid4()),  # not the deleted classroom
        "data": {"classroom_id": room, "org_id": "o"},
    }
    handle = handler(None)  # type: ignore[arg-type]  # never reached: the message is rejected first
    assert handle(CLASSROOM_DELETED, json.dumps(deletion).encode()) is False
    assert handle(CLASSROOM_DELETED, b"not json") is False
