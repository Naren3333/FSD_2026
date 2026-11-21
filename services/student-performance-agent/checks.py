"""the model's report, checked against what the agent actually gathered: anything citing evidence it never saw, or a misconception without a wrong answer the model read, is dropped and counted, never repaired."""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from workspace import Question, Workspace

MAX_OBSERVATIONS = 5
MAX_MISCONCEPTIONS = 3
MAX_INTERVENTIONS = 4
MAX_TOPICS = 3
MAX_TEXT = 500
# what looks like a reference ("S14", "T2", "Q5", "R1", "E3") in model-written text. only one the workspace
# gave out counts, since the teacher sees names, never references; a title like "T1 quiz" is ordinary text.
REFERENCE = re.compile(r"\b[STQRE]\d+\b")


class UnreadableOutputError(Exception):
    """the reply is not json in the report's shape."""


class RejectedError(Exception):
    """no observation survived the checks; `dropped` says why each part was dropped."""

    def __init__(self, dropped: Counter[str]) -> None:
        super().__init__("no observation survived")
        self.dropped = dropped


@dataclass
class Finding:
    text: str
    evidence: list[Question]


@dataclass
class Checked:
    observations: list[Finding]
    misconceptions: list[Finding]
    interventions: list[str]
    topic_ids: list[str]
    dropped: Counter[str] = field(default_factory=Counter)


def parse(output: str) -> dict[str, Any]:
    try:
        data = json.loads(output)
    except ValueError:
        raise UnreadableOutputError from None
    if not isinstance(data, dict):
        raise UnreadableOutputError
    return data


def report(data: dict[str, Any], workspace: Workspace, shown: set[str]) -> Checked:
    """the parts of a report that rest on gathered evidence; raises rejectederror when no observation does. `shown` is the evidence whose answers the reporting turn could see."""
    try:
        raw_observations = list(data["observations"])
        raw_misconceptions = list(data["possible_misconceptions"])
        raw_interventions = list(data["suggested_interventions"])
        raw_topics = list(data["recommended_topics"])
    except (KeyError, TypeError):
        raise UnreadableOutputError from None
    dropped: Counter[str] = Counter()
    questions = workspace.questions()
    observations = _findings(raw_observations, questions, shown, dropped, misconception=False)
    misconceptions = _findings(raw_misconceptions, questions, shown, dropped, misconception=True)
    if len(observations) > MAX_OBSERVATIONS:
        dropped["TOO_MANY"] += len(observations) - MAX_OBSERVATIONS
    if len(misconceptions) > MAX_MISCONCEPTIONS:
        dropped["TOO_MANY"] += len(misconceptions) - MAX_MISCONCEPTIONS
    interventions = []
    for text in raw_interventions:
        cleaned = clean(text)
        if cleaned and cleaned not in interventions:
            interventions.append(cleaned)
        else:
            dropped["EMPTY_OR_REPEATED"] += 1
    topic_ids: list[str] = []
    for ref in raw_topics:
        topic = workspace.topic_by_ref(ref)
        if topic is None:
            dropped["UNKNOWN_TOPIC"] += 1
        elif topic.topic_id not in topic_ids:
            topic_ids.append(topic.topic_id)
    if not observations:
        raise RejectedError(dropped)
    return Checked(
        observations[:MAX_OBSERVATIONS],
        misconceptions[:MAX_MISCONCEPTIONS],
        interventions[:MAX_INTERVENTIONS],
        topic_ids[:MAX_TOPICS],
        dropped,
    )


def clean(value: object) -> str:
    """a reported text with its whitespace collapsed and cut to max_text; empty when it is not a string."""
    return " ".join(value.split())[:MAX_TEXT] if isinstance(value, str) else ""


def strings(value: object) -> list[str]:
    """the distinct strings of a reported list, in order; empty when it is not a list."""
    return list(dict.fromkeys(r for r in value if isinstance(r, str))) if isinstance(value, list) else []


def misconception_drop(evidence: list[Question], shown: set[str]) -> str | None:
    """why a misconception citing these answers is dropped, or none when it may stand."""
    if any(q.ref not in shown for q in evidence):
        # it must rest on answers the model can still see, not on marks alone or a trimmed read.
        return "ANSWER_NOT_READ"
    if not all(q.lost_marks for q in evidence):
        return "FULL_MARKS_CITED"
    return None


def _findings(
    raw: list[Any],
    questions: dict[str, Question],
    shown: set[str],
    dropped: Counter[str],
    *,
    misconception: bool,
) -> list[Finding]:
    kept: list[Finding] = []
    for item in raw:
        text = clean(item.get("text")) if isinstance(item, dict) else ""
        refs = strings(item.get("evidence")) if isinstance(item, dict) else []
        if not text:
            dropped["EMPTY_OR_REPEATED"] += 1
        elif not refs:
            dropped["NO_EVIDENCE"] += 1
        elif any(r not in questions for r in refs):
            dropped["UNKNOWN_EVIDENCE"] += 1
        elif misconception and (why := misconception_drop([questions[r] for r in refs], shown)):
            dropped[why] += 1
        elif any(f.text == text for f in kept):
            dropped["EMPTY_OR_REPEATED"] += 1
        else:
            kept.append(Finding(text, [questions[r] for r in refs]))
    return kept
