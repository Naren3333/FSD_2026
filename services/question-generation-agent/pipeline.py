"""question generation and marking guidance (adr 0006): a fixed pipeline, not an agent."""

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from learning_transport import RELATION_TEACHER, Claims, UpstreamError, failure, require_relation
from learning_transport.agents import ai_core_refusal, unusable_reply

import checks
import prompts
import review
from clients import Services, billed
from config import Settings
from contracts import (
    Generated,
    GenerateRequest,
    GenerateResponse,
    Guidance,
    GuidanceRequest,
    Review,
    Round,
)
from prompts import Passage

log = logging.getLogger("question-generation-agent.pipeline")

MAX_QUERY = 1000  # content's longest search query
GENERATION_PASSAGES = 12
GUIDANCE_PASSAGES = 6
GENERATION_TOKENS = 6000
GUIDANCE_TOKENS = 2000
REVIEW_TOKENS = 3000
# a call with less time than this left before the deadline is not started.
MIN_CALL = 5.0
# a call the deadline stopped or kept from starting; not a failure to report, only the end of the retries.
OUT_OF_TIME = "OUT_OF_TIME"
WEAK_DISTRACTORS = "WEAK_DISTRACTORS"
UNREADABLE_REPLY = "UNREADABLE_REPLY"
NOT_TEACHER = ("FORBIDDEN", "Only the classroom's teacher can generate questions or marking guidance.")


def refused(e: UpstreamError) -> HTTPException:
    """a failed upstream call as an error the editor can act on, with a safe message."""
    if (known := ai_core_refusal(e)) is not None:
        return known
    if e.code == "RERANK_UNAVAILABLE":
        return failure(
            503, e.code, "Ranking the class materials is not available right now. Try again later."
        )
    if e.service == "content":
        if e.status in (401, 403):
            return failure(403, "FORBIDDEN", "You no longer have access to this classroom's materials.")
        return failure(503, "SEARCH_UNAVAILABLE", "Searching the class materials failed. Try again later.")
    if e.service in ("curriculum", "keycloak") or e.status in (0, 503):
        return failure(
            503, "AI_UNAVAILABLE", "Question generation is not available right now. Try again later."
        )
    return failure(502, "AI_PROVIDER_FAILED", "The model could not answer. Try again.")


def _round(
    generated: int,
    kept: list[Generated],
    counts: Counter[str],
    sent_back: Counter[str],
    passed_review: int,
    reviewed: bool = False,
) -> Round:
    return Round(
        generated=generated,
        passed_checks=len(kept),
        passed_review=passed_review,
        rejected=dict(counts),
        sent_back=dict(sent_back),
        reviewed=reviewed,
    )


def passages_of(results: list[dict[str, Any]], floor: float) -> list[Passage]:
    """the reranked search results as numbered passages; none when the best is below the relevance floor, since then the documents say too little about the request."""
    if not results or float(results[0].get("score", 0)) < floor:
        return []
    return [
        Passage(
            i,
            r["chunk_id"],
            r["document_id"],
            r.get("title") or "",
            int(r.get("page_start") or 1) if r.get("source", "pdf") in ("pdf", "vision") else None,
            r["content"],
            r.get("section_path"),
            r.get("source", "pdf"),
        )
        for i, r in enumerate(results, start=1)
        if r["content"].strip()
    ]


def not_run(n: int) -> Review:
    return Review(status="not_reviewed", note="The review could not run.", round=n)


def flag_long_key(draft: Generated) -> Generated:
    """a draft whose key gives itself away, flagged by our own check rather than the reviewer."""
    flagged = Review(status="flagged", note=checks.LONG_KEY_NOTE, source="check")
    return draft.model_copy(update={"review": flagged})


def omitted(n: int) -> Review:
    return Review(status="not_reviewed", note="The reviewer gave no verdict on this draft.", round=n)


@dataclass
class Run:
    """what a reviewed generation has so far."""

    accepted: list[Generated] = field(default_factory=list)  # passed the checks and the review
    unreviewed: list[Generated] = field(default_factory=list)  # passed the checks; the review did not run
    weak: list[tuple[Generated, str]] = field(
        default_factory=list
    )  # flagged drafts with their reason; kept if nothing better turns up
    attempts: list[checks.Rejected] = field(
        default_factory=list
    )  # every refused question, fed back to the generator
    dropped: Counter[str] = field(default_factory=Counter)
    rounds: list[Round] = field(default_factory=list)
    calls: int = 0
    stop_error: UpstreamError | None = None
    unreadable_retry: bool = False

    def shortfall(self, body: GenerateRequest) -> dict[str, int]:
        """how many more of each type are still wanted, leaving out the types that are full."""
        have = Counter(g.question.type for g in self.accepted + self.unreviewed)
        return {t: body.counts[t] - have[t] for t in body.question_types if body.counts[t] > have[t]}

    def next_request(self, body: GenerateRequest, shortfall: dict[str, int]) -> GenerateRequest:
        """the next round's request: the shortfall, with the drafts held as existing; weak and rejected drafts go back only as previous_attempts, so a corrected rewrite is not a duplicate."""
        held = [g.question.prompt for g in self.accepted + self.unreviewed]
        # may pass max_existing: the checks compare against all of them, and the prompts keep the newest that fit.
        return body.model_copy(
            update={"counts": shortfall, "existing_prompts": [*body.existing_prompts, *held]}
        )

    def checked(
        self, rejected: list[checks.Rejected], dropped: Counter[str], sent_back: Counter[str]
    ) -> None:
        """the checks' refusals: counted, and fed back (and counted in `sent_back`) unless writing them again would not help."""
        self.dropped.update(dropped)
        fed = [r for r in rejected if r.reason not in (checks.DUPLICATE, checks.WRONG_TYPE)]
        self.attempts += fed
        sent_back.update(r.reason for r in fed)

    def judged(
        self,
        n: int,
        kept: list[Generated],
        verdicts: list[review.Verdict],
        counts: Counter[str],
        sent_back: Counter[str],
    ) -> int:
        """sorts the round's drafts by verdict, adds the reviewer's reasons to `counts` and returns how many passed."""
        passed = 0
        for draft, verdict in zip(kept, verdicts, strict=True):
            if verdict.reason == review.NOT_REVIEWED:
                # no verdict is not a failure: the draft is kept, and a usable draft is not paid to be replaced.
                counts[review.NOT_REVIEWED] += 1
                self.unreviewed.append(draft.model_copy(update={"review": omitted(n)}))
                continue
            long_key = verdict.passed and checks.long_key(draft.question)
            if verdict.passed and not long_key:
                passed += 1
                self.accepted.append(draft.model_copy(update={"review": Review(status="passed", round=n)}))
                continue
            reason, note = (
                (checks.LONG_KEY, checks.LONG_KEY_NOTE) if long_key else (verdict.reason, verdict.note)
            )
            counts[reason] += 1
            self.attempts.append(checks.Rejected(draft.question.prompt, draft.question.type, reason))
            sent_back[reason] += 1
            if reason in (WEAK_DISTRACTORS, checks.LONG_KEY):
                flagged = Review(
                    status="flagged", note=note, round=n, source="check" if long_key else "reviewer"
                )
                self.weak.append((draft.model_copy(update={"review": flagged}), reason))
            else:
                self.dropped[verdict.reason] += 1
        return passed

    def assembled(self, body: GenerateRequest) -> list[Generated]:
        """by type: passed drafts, those not reviewed, then flagged ones newest round first, at most the count of each type; a flagged draft that rewrites a chosen one, or is cut, is counted as dropped."""
        final: list[Generated] = []
        for t in body.question_types:
            room = body.counts[t]
            chosen = [g for g in self.accepted + self.unreviewed if g.question.type == t][:room]
            flagged = sorted(
                ((g, reason) for g, reason in self.weak if g.question.type == t),
                key=lambda w: -w[0].review.round,
            )
            for g, reason in flagged:
                older = any(
                    checks.similar(g.question.prompt, c.question.prompt, checks.REWRITE_RATIO) for c in chosen
                )
                if len(chosen) >= room or older:
                    self.dropped[reason] += 1
                else:
                    chosen.append(g)
            final += chosen
        return final


class Pipeline:
    def __init__(self, services: Services, request: Request, claims: Claims, settings: Settings) -> None:
        self.services, self.request, self.claims, self.settings = services, request, claims, settings
        self.floor = settings.relevance_floor
        self.authorization = request.headers["authorization"]
        self.correlation: str = request.state.correlation_id

    async def generate(self, body: GenerateRequest) -> GenerateResponse:
        started = time.monotonic()
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
        except UpstreamError as e:
            log.warning("generation_failed", extra={"error": str(e)})
            raise refused(e) from None
        with_review = self.settings.review and body.review
        return await self._rounds(topic.name, body, passages, started, with_review)

    async def _ask(
        self,
        messages: list[dict[str, str]],
        tokens: int,
        schema: dict[str, Any],
        name: str,
        deadline: float | None = None,
    ) -> str:
        """one model call; with a deadline it is cut off then, so the request ends before the gateway gives up."""
        timeout = None
        if deadline is not None:
            timeout = deadline - time.monotonic()
            if timeout < MIN_CALL:
                raise UpstreamError("ai-core", 0, OUT_OF_TIME)
        try:
            return await self.services.generate(
                messages, tokens, schema, name, self.correlation, billed(self.claims), timeout
            )
        except UpstreamError as e:
            # an unanswered call that ran until the deadline was cut off by it, not by ai-core.
            if deadline is not None and e.status == 0 and time.monotonic() >= deadline - MIN_CALL:
                raise UpstreamError("ai-core", 0, OUT_OF_TIME) from None
            raise

    async def _rounds(
        self, topic: str, body: GenerateRequest, passages: list[Passage], started: float, with_review: bool
    ) -> GenerateResponse:
        """generate, review and retry the shortfall (adr 0006); without a review, one round of checks only. a retry needs 1.5 times the longest round to fit in review_deadline, and every call after the first generation is cut off at it."""
        run = Run()
        deadline = started + self.settings.review_deadline
        round_started, longest = started, 0.0
        for n in range(self.settings.review_retries + 1 if with_review else 1):
            now = time.monotonic()
            shortfall = run.shortfall(body)
            if n > 0:
                longest = max(longest, now - round_started)
                if not shortfall or now - started + 1.5 * longest > self.settings.review_deadline:
                    break
                run.rounds[-1].retried = True
            round_started = now
            request = run.next_request(body, shortfall)
            try:
                output = await self._ask(
                    prompts.generation_messages(topic, request, passages, run.attempts),
                    GENERATION_TOKENS,
                    prompts.generation_schema(request.question_types),
                    "questions.generated",
                    deadline if n > 0 else None,
                )
            except UpstreamError as e:
                log.warning("generation_failed", extra={"error": str(e), "round": n})
                if n == 0:
                    raise refused(e) from None
                run.stop_error = None if e.code == OUT_OF_TIME else e
                break
            run.calls += 1
            try:
                kept, dropped, rejected, generated = checks.questions(output, request, passages)
            except checks.UnreadableOutputError:
                if n == 0:
                    raise unusable_reply() from None
                run.rounds.append(
                    Round(generated=0, passed_checks=0, passed_review=0, rejected={UNREADABLE_REPLY: 1})
                )
                run.unreadable_retry = True
                break
            counts, sent_back = dropped, Counter[str]()
            run.checked(rejected, dropped, sent_back)
            if not kept:
                run.rounds.append(_round(generated, kept, counts, sent_back, 0))
                continue
            if not with_review:
                run.unreviewed += [flag_long_key(g) if checks.long_key(g.question) else g for g in kept]
                run.rounds.append(_round(generated, kept, counts, sent_back, len(kept)))
                continue
            try:
                verdicts = await self._review(topic, body, kept, passages, run, deadline)
            except (UpstreamError, checks.UnreadableOutputError) as e:
                log.warning("review_failed", extra={"error": str(e), "round": n})
                run.unreviewed += [g.model_copy(update={"review": not_run(n)}) for g in kept]
                run.rounds.append(_round(generated, kept, counts, sent_back, len(kept)))
                run.stop_error = e if isinstance(e, UpstreamError) and e.code != OUT_OF_TIME else None
                break
            passed = run.judged(n, kept, verdicts, counts, sent_back)
            run.rounds.append(_round(generated, kept, counts, sent_back, passed, reviewed=True))
        return self._response(body, run)

    def _response(self, body: GenerateRequest, run: Run) -> GenerateResponse:
        final = run.assembled(body)
        log.info(
            "generated",
            extra={
                "requested": body.count,
                "returned": len(final),
                "rounds": len(run.rounds),
                "calls": run.calls,
                "dropped": dict(run.dropped),
            },
        )
        if not final:
            if run.stop_error is not None:
                raise refused(run.stop_error)
            if run.unreadable_retry:
                raise unusable_reply()
            raise failure(
                422,
                "GENERATION_REJECTED",
                "None of the generated questions passed the checks. Try another focus or other documents.",
            )
        return GenerateResponse(
            questions=final,
            requested=body.count,
            requested_by_type={t: n for t, n in body.counts.items()},
            returned=len(final),
            dropped=dict(run.dropped),
            rounds=run.rounds,
            model_calls=run.calls,
        )

    async def _review(
        self,
        topic: str,
        body: GenerateRequest,
        kept: list[Generated],
        passages: list[Passage],
        run: Run,
        deadline: float,
    ) -> list[review.Verdict]:
        """one reviewer call over the drafts that passed the checks; it sees the editor's questions and those held so far."""
        output = await self._ask(
            prompts.review_messages(
                topic,
                body.difficulty,
                kept,
                passages,
                [*body.existing_prompts, *(g.question.prompt for g in run.accepted + run.unreviewed)],
            ),
            REVIEW_TOKENS,
            prompts.review_schema(),
            "questions.reviewed",
            deadline,
        )
        run.calls += 1
        return review.verdicts(output, len(kept))

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
            raise unusable_reply() from None
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
            # without the reranker the results are in rrf order and the floor can't be applied, so the
            # drafts would rest on passages nobody scored.
            raise UpstreamError("content", 503, "RERANK_UNAVAILABLE")
        passages = passages_of(results, self.floor)
        if not passages:
            raise failure(
                422,
                "INSUFFICIENT_SOURCE",
                "The chosen documents say too little about this. Choose other documents or change the focus.",
            )
        return prompts.fitting(passages)
