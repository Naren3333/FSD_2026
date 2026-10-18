"""Rewrites the model's source tags into the frontend's citation markers while the answer streams. Ported from
fsd_test's assistant.

The prompt labels documents S1…S8; models copy those reliably, where 36-char UUIDs get mangled. `[S3]` becomes
`[doc:<chunk_id>]`, and a list `[S1, S3]` becomes `[doc:a][doc:b]`. A tag that wasn't in the prompt, or a range
(`[S1-S3]`), is dropped and logged. Text after an unclosed `[` is held back until it closes, stops looking like
a tag, or passes HOLD chars, so a tag split across deltas is never relayed half-rewritten.
"""

import logging
import re

log = logging.getLogger("question-chatbot-agent.citations")

HOLD = 24  # longest held-back run, `[` included
_LIST = re.compile(r"\[(S\d+(?:\s*[,;]\s*S\d+)*)\]")
_RANGE = re.compile(r"\[S\d+\s*[-–]\s*S?\d+\]")
_TAG_CHARS = set("0123456789S ,;-–")


class CitationRewriter:
    def __init__(self, tag_map: dict[str, str]) -> None:
        """`tag_map`: "S1" → chunk id, for the documents that were in the prompt."""
        self.tag_map = tag_map
        self.cited: list[str] = []  # chunk ids in order of first citation
        self._held = ""

    def feed(self, delta: str) -> str:
        out: list[str] = []
        for ch in delta:
            if self._held:
                if ch == "]":
                    out.append(self._resolve(self._held + ch))
                    self._held = ""
                    continue
                if len(self._held) < HOLD and (ch == "S" if self._held == "[" else ch in _TAG_CHARS):
                    self._held += ch
                    continue
                out.append(self._held)  # not a tag after all
                self._held = ""
            if ch == "[":
                self._held = ch
            else:
                out.append(ch)
        return "".join(out)

    def flush(self) -> str:
        """The held-back text at the end of the stream, as written."""
        held, self._held = self._held, ""
        return held

    def _resolve(self, tag: str) -> str:
        if m := _LIST.fullmatch(tag):
            markers = []
            for label in re.split(r"\s*[,;]\s*", m[1]):
                chunk_id = self.tag_map.get(label)
                if chunk_id is None:
                    log.warning("citation_dropped_unknown_source", extra={"label": label})
                    continue
                if chunk_id not in self.cited:
                    self.cited.append(chunk_id)
                markers.append(f"[doc:{chunk_id}]")
            return "".join(markers)
        if _RANGE.fullmatch(tag):
            log.warning("citation_range_dropped", extra={"tag": tag})
            return ""
        return tag  # e.g. "[S]": tag-like, but not a citation
