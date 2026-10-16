import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from domain import Evidence, SkillMetric, summarize


def row(skill: str, correct: bool, awarded: int | None = None, possible: int = 1) -> dict[str, object]:
    """An evidence row as the database returns it."""
    return {
        "skill_id": skill,
        "correct": correct,
        "awarded": int(correct) if awarded is None else awarded,
        "possible": possible,
    }


def test_no_fabricated_metrics():
    assert summarize([]) == []


def test_observed_accuracy_and_insufficient_evidence():
    data = [row("fractions", n < 6) for n in range(10)]
    assert summarize(data)[0]["observed_accuracy"] == 0.6
    assert summarize(data[:2])[0]["evidence_status"] == "insufficient_evidence"
    assert "mastery" not in summarize(data)[0]


def test_reject_inconsistent_evidence():
    with pytest.raises(ValidationError):
        Evidence(question_id=uuid4(), skill_id=uuid4(), correct=True, awarded=0, possible=1)


def test_response_contract_rejects_invented_accuracy():
    metric = summarize([row(str(uuid4()), True)])[0]
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


def test_class_performance_keeps_roster_order_and_students_without_evidence():
    from domain import ClassPerformanceResponse, by_student

    skill = str(uuid4())
    rows = [{"student_id": "s2", **row(skill, n < 4)} for n in range(5)]
    students = by_student(["s1", "s2"], rows)
    assert [s["student_id"] for s in students] == ["s1", "s2"]
    assert students[0] == {"student_id": "s1", "skills": [], "evidence_status": "no_finalized_evidence"}
    assert students[1]["skills"][0]["observed_accuracy"] == 0.8
    page = {
        "classroom_id": str(uuid4()),
        "total": 2,
        "offset": 0,
        "students": students,
        "interpretation": "x",
    }
    assert ClassPerformanceResponse.model_validate(page).total == 2


def test_part_marks_show_in_the_marks_not_the_correct_count():
    metric = summarize([row("essay", False, 2, 5), row("essay", True, 3, 3)])[0]
    assert metric["attempted"] == 2 and metric["correct"] == 1
    assert (metric["marks_awarded"], metric["marks_possible"]) == (5, 8)
    with pytest.raises(ValidationError):
        SkillMetric.model_validate({**metric, "marks_awarded": 9})


def test_v2_evidence_may_be_worth_more_than_one_point():
    assert Evidence(question_id=uuid4(), skill_id=uuid4(), correct=False, awarded=2, possible=5).awarded == 2
    with pytest.raises(ValidationError):
        Evidence(question_id=uuid4(), skill_id=uuid4(), correct=False, awarded=6, possible=5)


def finalized(event_type: str, possible: int) -> dict[str, object]:
    evaluation = str(uuid4())
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "timestamp": "2026-10-16T08:00:00Z",
        "correlation_id": str(uuid4()),
        "resource_id": evaluation,
        "data": {
            "evaluation_id": evaluation,
            "assessment_id": str(uuid4()),
            "classroom_id": str(uuid4()),
            "org_id": "org",
            "student_id": "s1",
            "evidence": [
                {
                    "question_id": str(uuid4()),
                    "skill_id": str(uuid4()),
                    "correct": True,
                    "awarded": possible,
                    "possible": possible,
                }
            ],
        },
    }


def test_both_finalized_versions_are_read_and_v1_keeps_one_point():
    from domain import Event

    assert Event.model_validate(finalized("grading.finalized.v2", 4)).data.evidence[0].possible == 4
    assert Event.model_validate(finalized("grading.finalized.v1", 1)).data.evidence[0].possible == 1
    with pytest.raises(ValidationError):
        Event.model_validate(finalized("grading.finalized.v1", 4))
