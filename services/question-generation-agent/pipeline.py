"""Question generation and marking guidance (ADR 0006): a fixed pipeline, not an agent. Search the teacher's chosen
documents with the teacher's own token, make one structured ai-core call billed to the teacher, check the reply.
The model gets no tools, and nothing it writes is saved: the teacher reviews it in the editor."""

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Request
from learning_transport import RELATION_TEACHER, Claims, UpstreamError, failure, require_relation

import checks
import prompts
from clients import Services, billed
from contracts import GenerateRequest, GenerateResponse, Guidance, GuidanceRequest
from prompts import Passage

log = logging.getLogger("question-generation-agent.pipeline")

MAX_QUERY = 1000  # Content's longest search query
GENERATION_PASSAGES = 12
GUIDANCE_PASSAGES = 6
GENERATION_TOKENS = 6000
GUIDANCE_TOKENS = 2000
NOT_TEACHER = ("FORBIDDEN", "Only the classroom's teacher can generate questions or marking guidance.")


# Errors whose code alone says what the teacher should hear.
BY_CODE = {
    "AI_BUSY": (429, "You have too many AI requests running at once. Try again in a moment."),
    "AI_PROVIDER_NOT_CONFIGURED": (503, "AI is not set up on this server."),
    "AI_OUTPUT_REJECTED": (502, "The model did not return a usable reply. Try again."),
    "RERANK_UNAVAILABLE": (503, "Ranking the class materials is not available right now. Try again later."),
}


def refused(e: UpstreamError) -> HTTPException:
    """A failed upstream call as an error the editor can act on, with a safe message."""
    if e.code == "AI_SPEND_LIMIT":
        when = _local_time(e.resets_at)
        message = "You've used today's AI allowance." + (f" It resets at {when}." if when else "")
        return failure(429, "AI_SPEND_LIMIT", message)
    if e.code in BY_CODE:
        return failure(BY_CODE[e.code][0], e.code, BY_CODE[e.code][1])
    if e.service == "content":
        if e.status in (401, 403):
            return failure(403, "FORBIDDEN", "You no longer have access to this classroom's materials.")
        return failure(503, "SEARCH_UNAVAILABLE", "Searching the class materials failed. Try again later.")
    if e.service in ("curriculum", "keycloak") or e.status in (0, 503):
        return failure(
            503, "AI_UNAVAILABLE", "Question generation is not available right now. Try again later."
        )
    return failure(502, "AI_PROVIDER_FAILED", "The model could not answer. Try again.")


def _local_time(iso: str | None) -> str:
    """ai-core's reset time in its own zone (SPEND_TIMEZONE), with the zone's offset, e.g. "00:00 on 19 Oct
    (UTC+08:00)": the teacher's browser may be elsewhere."""
    try:
        at = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return ""
    if at is None:
        return ""
    zone = at.strftime(" (%Z)") if at.tzinfo is not None else ""
    return at.strftime("%H:%M on %d %b") + zone


def passages_of(results: list[dict[str, Any]], floor: float) -> list[Passage]:
    """The reranked search results as numbered passages; none when the best is below the relevance floor, since
    then the documents say too little about the request."""
    if not results or float(results[0].get("score", 0)) < floor:
        return []
    return [
        Passage(
            i,
            r["chunk_id"],
            r["document_id"],
            r.get("title") or "",
            int(r.get("page_start") or 1),
            r["content"],
        )
        for i, r in enumerate(results, start=1)
        if r["content"].strip()
    ]


class Pipeline:
    def __init__(self, services: Services, request: Request, claims: Claims, floor: float) -> None:
        self.services, self.request, self.claims, self.floor = services, request, claims, floor
        self.authorization = request.headers["authorization"]
        self.correlation: str = request.state.correlation_id

    async def generate(self, body: GenerateRequest) -> GenerateResponse:
        classroom = str(body.classroom_id)
        await require_relation(
            self.services.identity, self.request, classroom, allowed=(RELATION_TEACHER,), denied=NOT_TEACHER
        )
        try:
            topic = await self.services.topic(self.authorization, str(body.topic_id), self.correlation)
            if topic is None:
                raise failure(422, "UNKNOWN_TOPIC", "Choose a topic from your curriculum.")
            query = f"{topic.name}. {body.focus}" if body.focus else topic.name
            passages = await self._passages(classroom, query[:MAX_QUERY], body, GENERATION_PASSAGES)
            messages = prompts.generation_messages(topic.name, body, passages)
            output = await self.services.generate(
                messages,
                GENERATION_TOKENS,
                prompts.generation_schema(body.question_types),
                "questions.generated",
                self.correlation,
                billed(self.claims),
            )
        except UpstreamError as e:
            log.warning("generation_failed", extra={"error": str(e)})
            raise refused(e) from None
        try:
            kept, dropped = checks.questions(output, body, passages)
        except checks.UnreadableOutputError:
            raise failure(
                502, "AI_OUTPUT_REJECTED", "The model did not return a usable reply. Try again."
            ) from None
        log.info(
            "generated", extra={"requested": body.count, "returned": len(kept), "dropped": dict(dropped)}
        )
        if not kept:
            raise failure(
                422,
                "GENERATION_REJECTED",
                "None of the generated questions passed the checks. Try another focus or other documents.",
            )
        return GenerateResponse(
            questions=kept, requested=body.count, returned=len(kept), dropped=dict(dropped)
        )

    async def guidance(self, body: GuidanceRequest) -> Guidance:
        classroom = str(body.classroom_id)
        await require_relation(
            self.services.identity, self.request, classroom, allowed=(RELATION_TEACHER,), denied=NOT_TEACHER
        )
        try:
            passages = await self._passages(classroom, body.prompt[:MAX_QUERY], body, GUIDANCE_PASSAGES)
            output = await self.services.generate(
                prompts.guidance_messages(body.prompt, body.type, body.points, passages),
                GUIDANCE_TOKENS,
                prompts.guidance_schema(),
                "marking.guidance",
                self.correlation,
                billed(self.claims),
            )
        except UpstreamError as e:
            log.warning("guidance_failed", extra={"error": str(e)})
            raise refused(e) from None
        try:
            return checks.guidance(output, body, passages)
        except checks.UnreadableOutputError:
            raise failure(
                502, "AI_OUTPUT_REJECTED", "The model did not return a usable reply. Try again."
            ) from None
        except checks.RuleBrokenError as d:
            log.info("guidance rejected", extra={"reason": d.reason})
            raise failure(
                422, "GUIDANCE_REJECTED", "The drafted guidance did not pass the checks. Try again."
            ) from None

    async def _passages(
        self, classroom: str, query: str, body: GenerateRequest | GuidanceRequest, top_k: int
    ) -> list[Passage]:
        documents = [str(d) for d in body.document_ids]
        results, fallback = await self.services.search(
            self.authorization, classroom, query, documents, top_k, self.correlation
        )
        if fallback and results:
            # Without the reranker the results are in RRF order and the floor can't be applied, so the drafts would
            # rest on passages nobody scored. Refuse before the paid call, as the chatbot does; the teacher can retry.
            raise UpstreamError("content", 503, "RERANK_UNAVAILABLE")
        passages = passages_of(results, self.floor)
        if not passages:
            raise failure(
                422,
                "INSUFFICIENT_SOURCE",
                "The chosen documents say too little about this. Choose other documents or change the focus.",
            )
        return prompts.fitting(passages)
