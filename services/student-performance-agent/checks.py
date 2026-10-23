"""the model's report, checked against what the agent actually gathered: anything citing evidence it never saw, or a misconception without a wrong answer the model read, is dropped and counted, never repaired."""

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from workspace import Question, Workspace

MAX_OBSERVATIONS = 5
MAX_MISCONCEPTIONS = 3
MAX_INTERVENTIONS = 4
MAX_TOPICS = 3
MAX_TEXT = 500


class UnreadableOutputError(Exception):
    """the reply is not json in the report's shape."""


class RejectedError(Exception):
    """no observation survived the checks."""


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


def report(data: dict[str, Any], workspace: Workspace) -> Checked:
    """the parts of a report that rest on gathered evidence; raises rejectederror when no observation does."""
    try:
        raw_observations = list(data["observations"])
        raw_misconceptions = list(data["possible_misconceptions"])
        raw_interventions = list(data["suggested_interventions"])
        raw_topics = list(data["recommended_topics"])
    except (KeyError, TypeError):
        raise UnreadableOutputError from None
    dropped: Counter[str] = Counter()
    questions = workspace.questions()
    observations = _findings(raw_observations, questions, workspace, dropped, misconception=False)
    misconceptions = _findings(raw_misconceptions, questions, workspace, dropped, misconception=True)
    if len(observations) > MAX_OBSERVATIONS:
        dropped["TOO_MANY"] += len(observations) - MAX_OBSERVATIONS
    if len(misconceptions) > MAX_MISCONCEPTIONS:
        dropped["TOO_MANY"] += len(misconceptions) - MAX_MISCONCEPTIONS
    interventions = []
    for text in raw_interventions:
        cleaned = _text(text)
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
        raise RejectedError
    return Checked(
        observations[:MAX_OBSERVATIONS],
        misconceptions[:MAX_MISCONCEPTIONS],
        interventions[:MAX_INTERVENTIONS],
        topic_ids[:MAX_TOPICS],
        dropped,
    )


def _text(value: object) -> str:
    return " ".join(value.split())[:MAX_TEXT] if isinstance(value, str) else ""


def _findings(
    raw: list[Any],
    questions: dict[str, Question],
    workspace: Workspace,
    dropped: Counter[str],
    *,
    misconception: bool,
) -> list[Finding]:
    kept: list[Finding] = []
    for item in raw:
        text = _text(item.get("text")) if isinstance(item, dict) else ""
        refs = item.get("evidence") if isinstance(item, dict) else None
        refs = list(dict.fromkeys(r for r in refs if isinstance(r, str))) if isinstance(refs, list) else []
        if not text:
            dropped["EMPTY_OR_REPEATED"] += 1
        elif not refs:
            dropped["NO_EVIDENCE"] += 1
        elif any(r not in questions for r in refs):
            dropped["UNKNOWN_EVIDENCE"] += 1
        elif misconception and any(r not in workspace.read for r in refs):
            # a misconception must rest on answers the model actually read, not on marks alone.
            dropped["ANSWER_NOT_READ"] += 1
        elif misconception and not all(questions[r].lost_marks for r in refs):
            dropped["FULL_MARKS_CITED"] += 1
        elif any(f.text == text for f in kept):
            dropped["EMPTY_OR_REPEATED"] += 1
        else:
            kept.append(Finding(text, [questions[r] for r in refs]))
    return kept
