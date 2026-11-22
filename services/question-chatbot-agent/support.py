"""the answer support check's text handling (adr 0025): an answer's cited statements, the check's verdicts and the notices shown after the answer."""

import json
import re
from typing import Any

MAX_STATEMENTS = 24  # cited statements checked per answer; later ones are left unchecked
STATEMENT_CHARS = 300  # of a statement's text sent and saved
CLAIM_CHARS = 80  # of a claim's text named in the notice
NOTICE_CLAIMS = 3
VERDICTS = ("supported", "partial", "unsupported")
UNCITED_NOTICE = "\n\nThis answer does not cite the materials."

_MARKER = re.compile(r"\[doc:([0-9a-fA-F-]+)\]")
# a sentence ends at . ! or ? and the citations right after it, or at a line break.
_BOUNDARY = re.compile(r"((?<=[.!?])(?:[ \t]*\[doc:[0-9a-fA-F-]+\])*\s+|\n+)")
_MARKUP = re.compile(r"^\s*(?:[-*+]|\d+[.)]|#+)\s+|[*_`|#>]")
# the model saying the materials do not cover something (policy rule 2), which needs no citation.
_NOT_FOUND = re.compile(
    r"\b(?:could ?n[o’']t|can ?n[o’']t|cannot|unable to|did ?n[o’']t|do ?n[o’']t|does ?n[o’']t|not)\b"
    r".{0,40}\b(?:find|found|cover|covered|contain|include|mention|mentioned)\b",
    re.IGNORECASE,
)
FACTUAL_WORDS = 6  # a declarative sentence this long is taken as stating something


def _plain(text: str) -> str:
    return re.sub(r"\s+([.,;:!?])", r"\1", " ".join(_MARKUP.sub("", _MARKER.sub("", text)).split()))


def _sentences(text: str) -> list[tuple[str, list[str]]]:
    """the text's sentences, without citation markers and markup, each with the chunk ids it cites."""
    parts = _BOUNDARY.split(text)
    out: list[tuple[str, list[str]]] = []
    for n in range(0, len(parts), 2):
        cited = _MARKER.findall(parts[n] + (parts[n + 1] if n + 1 < len(parts) else ""))
        plain = _plain(parts[n])
        if len(plain.split()) < 2:
            if cited and out:  # a citation on its own after a sentence belongs to it
                out[-1][1].extend(c for c in cited if c not in out[-1][1])
            continue
        out.append((plain, list(dict.fromkeys(cited))))
    return out


def statements(answer: str) -> list[tuple[str, list[str]]]:
    """the answer's statements that cite passages, with their chunk ids: at most MAX_STATEMENTS."""
    return [(text[:STATEMENT_CHARS], cited) for text, cited in _sentences(answer) if cited][:MAX_STATEMENTS]


def factual(answer: str) -> bool:
    """whether an uncited answer states something: a declarative sentence, not a question or a statement that the materials don't cover it."""
    sentences = [text for text, _ in _sentences(answer)]
    if any(_NOT_FOUND.search(s) for s in sentences):
        return False
    return any(not s.endswith("?") and len(s.split()) >= FACTUAL_WORDS for s in sentences)


def faults(output: str, ids: list[str]) -> list[str]:
    """what is wrong with an answer_check.v1 reply: unreadable, or not one verdict per statement id."""
    try:
        verdicts = json.loads(output)["verdicts"]
        given = [str(v["id"]) for v in verdicts]
        if any(v["verdict"] not in VERDICTS for v in verdicts):
            return ["every verdict must be supported, partial or unsupported"]
    except (ValueError, KeyError, TypeError):
        return ["the reply must be a json object with a verdicts list"]
    errors = []
    if missing := [i for i in ids if i not in given]:
        errors.append("no verdict for statements " + ", ".join(missing))
    if extra := sorted({i for i in given if i not in ids or given.count(i) > 1}):
        errors.append("unknown or repeated statement ids " + ", ".join(extra))
    return errors


def verdicts(output: str) -> dict[str, str]:
    """statement id → verdict, from a reply that passed faults()."""
    return {str(v["id"]): str(v["verdict"]) for v in json.loads(output)["verdicts"]}


def unsupported_notice(claims: list[str]) -> str:
    """a note naming the statements the cited passages do not support."""
    named = []
    for claim in claims[:NOTICE_CLAIMS]:
        # answer text is model output: no markup or quotes of its own in the note.
        text = re.sub(r"[\[\]<>`*_“”\"]", "", claim)
        if len(text) > CLAIM_CHARS:
            text = text[:CLAIM_CHARS].rsplit(" ", 1)[0] + "…"
        named.append(f"“{text}”")
    more = len(claims) - NOTICE_CLAIMS
    rest = f" and {more} more" if more > 0 else ""
    return "\n\nThe cited passages may not support: " + "; ".join(named) + rest + "."


def record(checked: list[tuple[str, list[str]]], found: dict[str, str], version: str) -> dict[str, Any]:
    """the saved answer_check of a checked answer: each statement's verdict and cited chunks, and the counts."""
    rows = [
        {"statement": text, "chunk_ids": cited, "verdict": found[str(n)]}
        for n, (text, cited) in enumerate(checked, 1)
    ]
    counts = {v: sum(r["verdict"] == v for r in rows) for v in VERDICTS}
    return {"status": "checked", "prompt_version": version, "counts": counts, "statements": rows}
