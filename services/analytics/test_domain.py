import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from domain import LEGACY_TOPIC_FIELD, Evidence, TopicMetric, summarize


def row(topic: str, correct: bool, awarded: int | None = None, possible: int = 1) -> dict[str, object]:
    """An evidence row as the database returns it."""
    return {
        "topic_id": topic,
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
        Evidence(question_id=uuid4(), topic_id=uuid4(), correct=True, awarded=0, possible=1)


def test_response_contract_rejects_invented_accuracy():
    metric = summarize([row(str(uuid4()), True)])[0]
    assert TopicMetric.model_validate(metric).observed_accuracy == 1
    with pytest.raises(ValidationError):
        TopicMetric.model_validate({**metric, "observed_accuracy": 0.9})


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

    topic = str(uuid4())
    rows = [{"student_id": "s2", **row(topic, n < 4)} for n in range(5)]
    students = by_student(["s1", "s2"], rows)
    assert [s["student_id"] for s in students] == ["s1", "s2"]
    assert students[0] == {"student_id": "s1", "topics": [], "evidence_status": "no_finalized_evidence"}
    assert students[1]["topics"][0]["observed_accuracy"] == 0.8
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
        TopicMetric.model_validate({**metric, "marks_awarded": 9})


def test_v2_evidence_may_be_worth_more_than_one_point():
    assert Evidence(question_id=uuid4(), topic_id=uuid4(), correct=False, awarded=2, possible=5).awarded == 2
    with pytest.raises(ValidationError):
        Evidence(question_id=uuid4(), topic_id=uuid4(), correct=False, awarded=6, possible=5)


def finalized(event_type: str, possible: int) -> dict[str, object]:
    """A finalized event as that version publishes it: v1 and v2 name the topic by the legacy field."""
    evaluation = str(uuid4())
    field = "topic_id" if event_type == "grading.finalized.v3" else LEGACY_TOPIC_FIELD
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
                    field: str(uuid4()),
                    "correct": True,
                    "awarded": possible,
                    "possible": possible,
                }
            ],
        },
    }


def test_every_finalized_version_is_read_and_v1_keeps_one_point():
    from domain import Event

    v3 = finalized("grading.finalized.v3", 4)
    topic = v3["data"]["evidence"][0]["topic_id"]  # type: ignore[index]
    assert str(Event.model_validate(v3).data.evidence[0].topic_id) == topic
    assert Event.model_validate(finalized("grading.finalized.v2", 4)).data.evidence[0].possible == 4
    assert Event.model_validate(finalized("grading.finalized.v1", 1)).data.evidence[0].possible == 1
    with pytest.raises(ValidationError):
        Event.model_validate(finalized("grading.finalized.v1", 4))


def test_old_versions_map_their_topic_field_and_each_version_keeps_its_own_field():
    from domain import Event

    for version in ("grading.finalized.v1", "grading.finalized.v2"):
        old = finalized(version, 1)
        topic = old["data"]["evidence"][0][LEGACY_TOPIC_FIELD]  # type: ignore[index]
        assert str(Event.model_validate(old).data.evidence[0].topic_id) == topic
        evidence = old["data"]["evidence"][0]  # type: ignore[index]
        evidence["topic_id"] = evidence.pop(LEGACY_TOPIC_FIELD)
        with pytest.raises(ValidationError):
            Event.model_validate(old)
    v3 = finalized("grading.finalized.v3", 1)
    evidence = v3["data"]["evidence"][0]  # type: ignore[index]
    evidence[LEGACY_TOPIC_FIELD] = evidence.pop("topic_id")
    with pytest.raises(ValidationError):
        Event.model_validate(v3)


def test_a_duplicate_v3_event_is_stored_once():
    from consumer import process
    from domain import Event

    event = Event.model_validate(finalized("grading.finalized.v3", 2))
    first, again = Recorder(), Recorder()
    process(first, event)  # type: ignore[arg-type]
    assert any("INSERT INTO evaluations" in s for s in first.statements)
    again.inboxed = True
    process(again, event)  # type: ignore[arg-type]
    assert len(again.statements) == 1 and "inbox" in again.statements[0]


def test_finalized_events_on_the_wrong_topic_are_dead_lettered():
    from consumer import handler

    handle = handler(None)  # type: ignore[arg-type]  # never reached: the message is rejected first
    v2 = json.dumps(finalized("grading.finalized.v2", 4)).encode()
    assert handle("grading.finalized.v1", v2) is False
    v1 = json.dumps(finalized("grading.finalized.v1", 1)).encode()
    assert handle("grading.finalized.v2", v1) is False
    v3 = json.dumps(finalized("grading.finalized.v3", 4)).encode()
    assert handle("grading.finalized.v2", v3) is False


def test_ids_longer_than_200_characters_are_valid():
    from domain import Event

    event = finalized("grading.finalized.v2", 1)
    event["data"]["org_id"] = event["data"]["student_id"] = "x" * 300  # type: ignore[index]
    assert Event.model_validate(event).data.student_id == "x" * 300


class Recorder:
    """A pool whose one connection records each statement and finds nothing, except a tombstone when `purged`."""

    def __init__(self, purged: bool = False) -> None:
        self.statements: list[str] = []
        self.purged = purged
        self.inboxed = False  # True: the event is already in the inbox

    def connection(self) -> "Recorder":
        return self

    def transaction(self) -> "Recorder":
        return self

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> "Recorder":
        self.statements.append(sql)
        return self

    def fetchone(self) -> dict[str, object] | None:
        # The inbox insert succeeds; nothing else is found but the tombstone, when there is one.
        if len(self.statements) == 1 and self.inboxed:
            return None
        if len(self.statements) == 1 or (self.purged and "purged_classrooms" in self.statements[-1]):
            return {"event_id": 1}
        return None

    def fetchall(self) -> list[object]:
        return []


def test_storing_and_purging_lock_the_classroom_before_its_tombstone():
    from learning_transport.db import LOCK

    from consumer import process, purge
    from domain import ClassroomDeletedEvent, Event

    stored = Recorder()
    process(stored, Event.model_validate(finalized("grading.finalized.v2", 1)))  # type: ignore[arg-type]
    assert stored.statements.index(LOCK) < next(
        i for i, s in enumerate(stored.statements) if "purged_classrooms" in s
    )
    room = str(uuid4())
    deletion = {
        "event_id": str(uuid4()),
        "event_type": "identity.classroom.deleted.v1",
        "schema_version": 1,
        "timestamp": "2026-10-14T00:00:00Z",
        "correlation_id": str(uuid4()),
        "resource_id": room,
        "data": {"classroom_id": room, "org_id": "o"},
    }
    purged = Recorder()
    purge(purged, ClassroomDeletedEvent.model_validate(deletion))  # type: ignore[arg-type]
    assert purged.statements[0] == LOCK


def test_consumer_group_can_be_overridden(monkeypatch):
    import consumer

    groups: list[str] = []
    monkeypatch.setattr(consumer, "consume", lambda group, *_: groups.append(group))
    monkeypatch.delenv("KAFKA_GROUP", raising=False)
    consumer.run(None, None, None)  # type: ignore[arg-type]
    monkeypatch.setenv("KAFKA_GROUP", "analytics-rebuild-1")
    consumer.run(None, None, None)  # type: ignore[arg-type]
    assert groups == ["analytics-v1", "analytics-rebuild-1"]


def test_the_consumer_reads_every_finalized_version(monkeypatch):
    import consumer

    topics: list[list[str]] = []
    monkeypatch.setattr(consumer, "consume", lambda group, subscribed, *_: topics.append(subscribed))
    consumer.run(None, None, None)  # type: ignore[arg-type]
    assert topics == [
        [
            "grading.finalized.v1",
            "grading.finalized.v2",
            "grading.finalized.v3",
            "identity.classroom.deleted.v1",
        ]
    ]


def test_the_weakness_threshold_is_below_sixty_percent_over_five_questions():
    at_threshold = summarize([row("s", n < 3) for n in range(5)])[0]
    assert (at_threshold["observed_accuracy"], at_threshold["evidence_status"]) == (0.6, "observed")
    assert at_threshold["weakness_indicator"] is False
    below = summarize([row("s", n < 2) for n in range(5)])[0]
    assert below["weakness_indicator"] is True
    too_few = summarize([row("s", False) for _ in range(4)])[0]
    assert too_few["evidence_status"] == "insufficient_evidence" and too_few["weakness_indicator"] is False


def test_topics_are_sorted_and_v1_and_v2_rows_add_up():
    a, b, c = sorted(str(uuid4()) for _ in range(3))
    rows = [row(c, True), row(a, False, 0, 4), row(b, True), row(a, True), row(a, True, 5, 5)]
    metrics = summarize(rows)
    assert [m["topic_id"] for m in metrics] == [a, b, c]
    assert (metrics[0]["attempted"], metrics[0]["correct"]) == (3, 2)
    assert (metrics[0]["marks_awarded"], metrics[0]["marks_possible"]) == (6, 10)
    assert all(TopicMetric.model_validate(m) for m in metrics)


def test_a_grade_for_a_purged_classroom_stores_nothing():
    from consumer import process
    from domain import Event

    recorder = Recorder(purged=True)
    process(recorder, Event.model_validate(finalized("grading.finalized.v2", 1)))  # type: ignore[arg-type]
    assert "inbox" in recorder.statements[0]
    assert not any("evaluations" in s or "evidence" in s for s in recorder.statements)
    stored = Recorder()
    process(stored, Event.model_validate(finalized("grading.finalized.v2", 1)))  # type: ignore[arg-type]
    assert any("INSERT INTO evaluations" in s for s in stored.statements)
