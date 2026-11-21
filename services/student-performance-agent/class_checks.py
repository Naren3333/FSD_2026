"""the class insights report, checked against what the agent gathered (adr 0021): a student named as struggling must have enough released evidence that shows it, every statement must rest on topics or answers the model was shown, a misconception on answers it read that lost marks, and every percentage in a text must be a share the model was shown about what the item cites. anything else is dropped and counted, never repaired."""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from checks import REFERENCE, RejectedError, UnreadableOutputError, clean, misconception_drop, strings
from class_workspace import ClassWorkspace, Student
from workspace import Question, Topic

MAX_STRUGGLING = 6
MAX_OBSERVATIONS = 5
MAX_MISCONCEPTIONS = 3
MAX_RETEACH = 4
# a percentage written in a text, and how far, in percentage points, it may sit from a share it was shown.
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|per\s?cent\b)", re.IGNORECASE)
TOLERANCE = 0.5


@dataclass
class Group:
    topic: Topic
    students: list[Student]
    text: str


@dataclass
class Statement:
    text: str
    topics: list[Topic] = field(default_factory=list)
    students: list[Student] = field(default_factory=list)
    evidence: list[Question] = field(default_factory=list)


@dataclass
class Checked:
    struggling: list[Group]
    observations: list[Statement]
    misconceptions: list[Statement]
    reteach: list[Statement]  # each with exactly one topic
    dropped: Counter[str] = field(default_factory=Counter)


def report(data: dict[str, Any], ws: ClassWorkspace, shown: set[str]) -> Checked:
    """the parts of a report that rest on gathered evidence; raises rejectederror when none does. `shown` is the evidence whose answers the reporting turn could see."""
    try:
        raw = {k: list(data[k]) for k in ("struggling", "observations", "possible_misconceptions", "reteach")}
    except (KeyError, TypeError):
        raise UnreadableOutputError from None
    dropped: Counter[str] = Counter()
    # groups are one per topic already; two topics may well share a sentence.
    struggling = _capped(_groups(raw["struggling"], ws, dropped), MAX_STRUGGLING, dropped, key=id)
    observations = _capped(
        [s for item in raw["observations"] if (s := _observation(item, ws, dropped))],
        MAX_OBSERVATIONS,
        dropped,
    )
    misconceptions = _capped(
        [s for item in raw["possible_misconceptions"] if (s := _misconception(item, ws, shown, dropped))],
        MAX_MISCONCEPTIONS,
        dropped,
    )
    # one reteach item per topic.
    reteach = _capped(
        [s for item in raw["reteach"] if (s := _reteach(item, ws, dropped))],
        MAX_RETEACH,
        dropped,
        key=lambda s: s.topics[0].topic_id,
    )
    if not (struggling or observations or misconceptions or reteach):
        raise RejectedError(dropped)
    return Checked(struggling, observations, misconceptions, reteach, dropped)


def percentages(text: str) -> list[float]:
    return [float(m) for m in PERCENT.findall(text)]


def supported(text: str, shares: set[float]) -> bool:
    """whether every percentage in the text is, within the tolerance, one of these shares; true with none."""
    return all(any(abs(p - 100 * s) <= TOLERANCE + 1e-9 for s in shares) for p in percentages(text))


def _wording_holds(
    text: str,
    ws: ClassWorkspace,
    dropped: Counter[str],
    topics: list[Topic],
    students: list[Student],
    evidence: list[Question],
) -> bool:
    """the text names no reference and its percentages rest on shares shown about its topics, students and answers; counted when not."""
    if any(ws.is_reference(r) for r in REFERENCE.findall(text)):
        dropped["REFERENCE_IN_TEXT"] += 1
        return False
    if not percentages(text):
        return True
    shares = ws.shares({t.topic_id for t in topics}, {s.student_id for s in students}, evidence)
    if supported(text, shares):
        return True
    dropped["UNSUPPORTED_FIGURE"] += 1
    return False


def _capped[T: Group | Statement](
    kept: list[T], limit: int, dropped: Counter[str], key: Callable[[T], object] = lambda s: s.text
) -> list[T]:
    """the first `limit`, repeats by `key` (the text, unless given) already gone; the rest are counted as too many."""
    unique: dict[object, T] = {}
    for item in kept:
        if key(item) in unique:
            dropped["EMPTY_OR_REPEATED"] += 1
        else:
            unique[key(item)] = item
    if len(unique) > limit:
        dropped["TOO_MANY"] += len(unique) - limit
    return list(unique.values())[:limit]


def _groups(raw: list[Any], ws: ClassWorkspace, dropped: Counter[str]) -> list[Group]:
    kept: list[Group] = []
    for raw_item in raw:
        item = raw_item if isinstance(raw_item, dict) else {}
        text, topic = clean(item.get("text")), ws.topic_by_ref(item.get("topic"))
        if not text:
            dropped["EMPTY_OR_REPEATED"] += 1
            continue
        if topic is None:
            dropped["UNKNOWN_TOPIC"] += 1
            continue
        if any(g.topic is topic for g in kept):
            dropped["EMPTY_OR_REPEATED"] += 1
            continue
        students = []
        for ref in strings(item.get("students")):
            student = ws.student_by_ref(ref)
            standing = ws.standing(student.student_id, topic.topic_id) if student else None
            if student is None:
                dropped["UNKNOWN_STUDENT"] += 1
            elif standing in ("none", "insufficient"):
                # too few released questions to call it a struggle: never named on so little.
                dropped["INSUFFICIENT_EVIDENCE"] += 1
            elif standing != "struggling":
                dropped["NOT_SUPPORTED"] += 1
            else:
                students.append(student)
        if not students:
            dropped["NO_STUDENTS"] += 1
        # a figure must rest on the students kept, not one dropped above; a wrong one drops the whole group.
        elif _wording_holds(text, ws, dropped, [topic], students, []):
            kept.append(Group(topic, students, text))
    return kept


def _refs(item: dict[str, Any], ws: ClassWorkspace, dropped: Counter[str]) -> Statement | None:
    """a statement's text and its cited topics, students and evidence; none, counted, when a reference is unknown or unseen."""
    text = clean(item.get("text"))
    if not text:
        dropped["EMPTY_OR_REPEATED"] += 1
        return None
    topics = [ws.topic_by_ref(r) for r in strings(item.get("topics"))]
    students = [ws.student_by_ref(r) for r in strings(item.get("students"))]
    refs = strings(item.get("evidence"))
    if any(t is None for t in topics):
        dropped["UNKNOWN_TOPIC"] += 1
    elif any(s is None for s in students):
        dropped["UNKNOWN_STUDENT"] += 1
    elif any(r not in ws.evidence or r not in ws.seen for r in refs):
        dropped["UNKNOWN_EVIDENCE"] += 1
    else:
        return Statement(
            text,
            [t for t in topics if t],
            [s for s in students if s],
            [ws.evidence[r] for r in refs],
        )
    return None


def _observation(item: object, ws: ClassWorkspace, dropped: Counter[str]) -> Statement | None:
    s = _refs(item if isinstance(item, dict) else {}, ws, dropped)
    if s is None:
        return None
    if not (s.topics or s.evidence):
        dropped["NO_EVIDENCE"] += 1
        return None
    # a student it names must have released evidence on a topic it cites, or one of its cited answers.
    topic_ids = {t.topic_id for t in s.topics}
    answered = {ws.owner[q.ref] for q in s.evidence}
    if any(not (topic_ids & set(st.topics) or st.student_id in answered) for st in s.students):
        dropped["NOT_SUPPORTED"] += 1
        return None
    return s if _wording_holds(s.text, ws, dropped, s.topics, s.students, s.evidence) else None


def _misconception(
    item: object, ws: ClassWorkspace, shown: set[str], dropped: Counter[str]
) -> Statement | None:
    s = _refs(item if isinstance(item, dict) else {}, ws, dropped)
    if s is None:
        return None
    if not s.evidence:
        dropped["NO_EVIDENCE"] += 1
    elif why := misconception_drop(s.evidence, shown):
        dropped[why] += 1
    elif _wording_holds(s.text, ws, dropped, [], [], s.evidence):
        return Statement(s.text, evidence=s.evidence)
    return None


def _reteach(item: object, ws: ClassWorkspace, dropped: Counter[str]) -> Statement | None:
    item = item if isinstance(item, dict) else {}
    topic = ws.topic_by_ref(item.get("topic"))
    if topic is None and clean(item.get("text")):
        dropped["UNKNOWN_TOPIC"] += 1
        return None
    s = _refs({"text": item.get("text"), "evidence": item.get("evidence")}, ws, dropped)
    if s is None or topic is None or not _wording_holds(s.text, ws, dropped, [topic], [], s.evidence):
        return None
    return Statement(s.text, topics=[topic], evidence=s.evidence)
