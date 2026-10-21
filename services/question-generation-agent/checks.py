"""deterministic checks on the model's replies (adr 0006)."""

import json
from collections import Counter
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from contracts import (
    DEFAULT_MAX_LENGTH,
    MAX_ACCEPTED,
    MAX_ANSWER,
    MAX_CRITERIA,
    MAX_CRITERION,
    MAX_DESCRIPTOR,
    MAX_EXCERPT_TEXT,
    MAX_LEVELS,
    MAX_MODEL_ANSWER,
    MAX_OPTIONS,
    MAX_POINTS,
    MAX_PROMPT,
    MAX_SOURCES,
    MIN_LEVELS,
    MIN_OPTIONS,
    Criterion,
    DraftQuestion,
    Excerpt,
    Generated,
    GenerateRequest,
    Guidance,
    GuidanceRequest,
    Level,
    Rubric,
)
from prompts import Passage

MIN_EVIDENCE = 20  # characters of quoted evidence, after normalizing
DUPLICATE_RATIO = 0.9  # prompts at least this similar are the same question

# reasons a question is dropped.
INVALID_PROMPT = "INVALID_PROMPT"
WRONG_TYPE = "WRONG_TYPE"
UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
INVALID_KEY = "INVALID_KEY"
INVALID_RUBRIC = "INVALID_RUBRIC"
INVALID_GUIDANCE = "INVALID_GUIDANCE"
DUPLICATE = "DUPLICATE"


class UnreadableOutputError(Exception):
    """the reply as a whole could not be read."""


class RuleBrokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize(text: str) -> str:
    """text as compared: typographic quotes made plain, whitespace collapsed, case folded."""
    for fancy, plain in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(fancy, plain)
    return " ".join(text.split()).casefold()


def read_reply(output: str) -> dict[str, Any]:
    try:
        reply = json.loads(output)
    except ValueError:
        raise UnreadableOutputError from None
    if not isinstance(reply, dict):
        raise UnreadableOutputError
    return reply


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _texts(item: dict[str, Any], key: str) -> list[str]:
    value = item.get(key, [])
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str)]


def cited(item: dict[str, Any], passages: list[Passage]) -> list[Passage]:
    """the passages the reply cites, in its order, each once."""
    numbers = item.get("source_passages")
    by_number = {p.number: p for p in passages}
    if not isinstance(numbers, list) or not numbers:
        raise RuleBrokenError(UNKNOWN_SOURCE)
    found: list[Passage] = []
    for n in numbers:
        if not isinstance(n, int) or isinstance(n, bool) or n not in by_number:
            raise RuleBrokenError(UNKNOWN_SOURCE)
        if by_number[n] not in found:
            found.append(by_number[n])
    return found


def evidenced_excerpts(evidence: str, sources: list[Passage]) -> list[Excerpt]:
    """the cited passages as the question stores them, at most max_sources of max_excerpt_text characters each."""
    quote = normalize(evidence)
    if len(quote) < MIN_EVIDENCE:
        raise RuleBrokenError(EVIDENCE_NOT_FOUND)
    texts = [_excerpt_text(p.text, quote) for p in sources]
    holding = [i for i, (_, found) in enumerate(texts) if found]
    if not holding:
        raise RuleBrokenError(EVIDENCE_NOT_FOUND)
    chosen = list(range(min(len(sources), MAX_SOURCES)))
    if holding[0] not in chosen:
        chosen = [holding[0], *chosen[:-1]]
    return [
        Excerpt(
            document_id=UUID(sources[i].document_id),
            chunk_id=UUID(sources[i].chunk_id),
            page=sources[i].page,
            text=texts[i][0],
        )
        for i in chosen
    ]


def _excerpt_text(text: str, quote: str) -> tuple[str, bool]:
    """at most max_excerpt_text characters of `text`, and whether they hold the normalized `quote`: the whole text when it fits, else the window centred on the quote, else (no quote in it) its start."""
    if len(text) <= MAX_EXCERPT_TEXT:
        return text, quote in normalize(text)
    folded, positions = _normalized_positions(text)
    at = folded.find(quote)
    if at < 0:
        return text[:MAX_EXCERPT_TEXT], False
    start, end = positions[at], positions[at + len(quote) - 1] + 1
    if end - start > MAX_EXCERPT_TEXT:
        return text[:MAX_EXCERPT_TEXT], False
    begin = min(max(start - (MAX_EXCERPT_TEXT - (end - start)) // 2, 0), len(text) - MAX_EXCERPT_TEXT)
    return text[begin : begin + MAX_EXCERPT_TEXT], True


def _normalized_positions(text: str) -> tuple[str, list[int]]:
    """normalize(text), and for each of its characters the index in `text` it came from."""
    out: list[str] = []
    positions: list[int] = []
    gap = False
    for i, ch in enumerate(text):
        if ch.isspace():
            gap = bool(out)
            continue
        if gap:
            out.append(" ")
            positions.append(i)
            gap = False
        for c in normalize(ch):
            out.append(c)
            positions.append(i)
    return "".join(out), positions


def points(item: dict[str, Any]) -> int:
    """the suggested marks, or 1 when the model gave none in range; the teacher sets them in the editor."""
    value = item.get("points")
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_POINTS else 1


def options(item: dict[str, Any]) -> tuple[list[str], str]:
    choices, answer = _texts(item, "options"), _text(item, "answer")
    if not MIN_OPTIONS <= len(choices) <= MAX_OPTIONS or len(set(choices)) != len(choices):
        raise RuleBrokenError(INVALID_KEY)
    if any(not c or len(c) > MAX_ANSWER for c in choices) or answer not in choices:
        raise RuleBrokenError(INVALID_KEY)
    return choices, answer


def accepted(item: dict[str, Any]) -> list[str]:
    answers = _texts(item, "accepted_answers")
    keys = [normalize(a) for a in answers]
    if not 1 <= len(answers) <= MAX_ACCEPTED or len(set(keys)) != len(keys):
        raise RuleBrokenError(INVALID_KEY)
    if any(not a or len(a) > MAX_ANSWER for a in answers):
        raise RuleBrokenError(INVALID_KEY)
    return answers


def rubric(item: dict[str, Any]) -> Rubric:
    """the rubric and model answer as assessment takes them: 1 to 10 criteria of 2 to 6 levels with their own points, adding up to at most 100 marks, and a model answer."""
    raw, model_answer = item.get("criteria"), _text(item, "model_answer")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CRITERIA:
        raise RuleBrokenError(INVALID_RUBRIC)
    criteria = []
    for c in raw:
        title = _text(c, "title") if isinstance(c, dict) else ""
        levels = c.get("levels") if isinstance(c, dict) else None
        if not title or len(title) > MAX_CRITERION or not isinstance(levels, list):
            raise RuleBrokenError(INVALID_RUBRIC)
        if not MIN_LEVELS <= len(levels) <= MAX_LEVELS:
            raise RuleBrokenError(INVALID_RUBRIC)
        parsed = []
        for level in levels:
            score = level.get("points") if isinstance(level, dict) else None
            descriptor = _text(level, "descriptor") if isinstance(level, dict) else ""
            if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= MAX_POINTS:
                raise RuleBrokenError(INVALID_RUBRIC)
            if not descriptor or len(descriptor) > MAX_DESCRIPTOR:
                raise RuleBrokenError(INVALID_RUBRIC)
            parsed.append(Level(points=score, descriptor=descriptor))
        if len({lv.points for lv in parsed}) != len(parsed):
            raise RuleBrokenError(INVALID_RUBRIC)
        criteria.append(Criterion(title=title, levels=sorted(parsed, key=lambda lv: lv.points)))
    # levels have distinct points from 0 up, so every criterion is worth at least 1 and only the top can
    # break.
    if rubric_total(criteria) > MAX_POINTS or not model_answer or len(model_answer) > MAX_MODEL_ANSWER:
        raise RuleBrokenError(INVALID_RUBRIC)
    return Rubric(criteria=criteria, model_answer=model_answer)


def rubric_total(criteria: list[Criterion]) -> int:
    """a rubric's marks: the sum of each criterion's top level, as assessment counts them."""
    return sum(max(lv.points for lv in c.levels) for c in criteria)


def marking_points(item: dict[str, Any]) -> Excerpt:
    text = _text(item, "marking_points")
    if not text or len(text) > MAX_EXCERPT_TEXT:
        raise RuleBrokenError(INVALID_GUIDANCE)
    return Excerpt(text=text)


def similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio() >= DUPLICATE_RATIO


def question(item: Any, request: GenerateRequest, passages: list[Passage], seen: list[str]) -> Generated:
    """one question of the reply, checked; raises rulebrokenerror with the first rule it breaks."""
    if not isinstance(item, dict):
        raise RuleBrokenError(INVALID_PROMPT)
    prompt, kind = _text(item, "prompt"), item.get("type")
    if not prompt or len(prompt) > MAX_PROMPT:
        raise RuleBrokenError(INVALID_PROMPT)
    if kind not in request.question_types:
        raise RuleBrokenError(WRONG_TYPE)
    sources = cited(item, passages)
    evidence = _text(item, "evidence")
    stored = evidenced_excerpts(evidence, sources)
    if any(similar(prompt, other) for other in seen):
        raise RuleBrokenError(DUPLICATE)
    base: dict[str, Any] = {"prompt": prompt, "type": kind, "topic_id": request.topic_id, "sources": stored}
    if kind == "multiple_choice":
        choices, answer = options(item)
        draft = DraftQuestion(**base, options=choices, answer=answer, points=points(item))
    elif kind == "short_answer":
        draft = DraftQuestion(
            **base,
            options=[],
            answer="",
            accepted_answers=accepted(item),
            points=points(item),
            marking_scheme=[marking_points(item)],
        )
    else:
        written = rubric(item)
        draft = DraftQuestion(
            **base,
            options=[],
            answer="",
            rubric=written,
            points=rubric_total(written.criteria),
            max_length=DEFAULT_MAX_LENGTH,
            marking_scheme=[marking_points(item)],
        )
    return Generated(question=draft, evidence=evidence, explanation=_text(item, "explanation"))


def questions(
    output: str, request: GenerateRequest, passages: list[Passage]
) -> tuple[list[Generated], Counter[str]]:
    """the reply's questions that pass every check, at most the number asked for, and the rest counted by reason."""
    items = read_reply(output).get("questions")
    if not isinstance(items, list):
        raise UnreadableOutputError
    kept: list[Generated] = []
    dropped: Counter[str] = Counter()
    seen = [p for p in request.existing_prompts if p.strip()]
    for item in items:
        if len(kept) == request.count:
            break
        try:
            generated = question(item, request, passages, seen)
        except RuleBrokenError as d:
            dropped[d.reason] += 1
            continue
        kept.append(generated)
        seen.append(generated.question.prompt)
    return kept, dropped


def guidance(output: str, request: GuidanceRequest, passages: list[Passage]) -> Guidance:
    """the drafted guidance, checked like a generated question; raises rulebrokenerror with the first rule it breaks."""
    item = read_reply(output)
    sources = cited(item, passages)
    evidence = _text(item, "evidence")
    stored = evidenced_excerpts(evidence, sources)
    short = request.type == "short_answer"
    return Guidance(
        type=request.type,
        accepted_answers=accepted(item) if short else [],
        rubric=None if short else rubric(item),
        marking_points=marking_points(item),
        sources=stored,
        evidence=evidence,
    )
