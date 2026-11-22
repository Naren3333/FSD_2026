"""question generation and marking guidance (adr 0006), with a reviewer pass that sends rejected drafts back for a bounded number of rewrites (adr 0015), and a student's own practice on the same loop (adr 0022)."""

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, Request
from learning_transport import (
    RELATION_STUDENT,
    RELATION_TEACHER,
    STUDENT,
    TEACHER,
    Claims,
    UpstreamError,
    failure,
    require_relation,
)
from learning_transport.agents import OUT_OF_TIME, ai_core_refusal, billed_to, unusable_reply

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
    Practicable,
    PracticeQuestion,
    PracticeRequest,
    PracticeResponse,
    Review,
    Round,
)
from prompts import Passage
from upstream import SearchPassage

log = logging.getLogger("question-generation-agent.pipeline")

MAX_QUERY = 1000  # content's longest search query
GENERATION_PASSAGES = 12
GUIDANCE_PASSAGES = 6
GENERATION_TOKENS = 6000
GUIDANCE_TOKENS = 2000
REVIEW_TOKENS = 3000
WEAK_DISTRACTORS = "WEAK_DISTRACTORS"
UNREADABLE_REPLY = "UNREADABLE_REPLY"
NOT_TEACHER = ("FORBIDDEN", "Only the classroom's teacher can generate questions or marking guidance.")
NOT_STUDENT = ("FORBIDDEN", "Only a student enrolled in this classroom can generate practice questions.")
# a practice set with no topic runs the pipeline under this id; it never leaves the agent (adr 0022).
NO_TOPIC = UUID(int=0)


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


def passages_of(results: list[SearchPassage], floor: float) -> list[Passage]:
    """the reranked search results at or above the relevance floor, as numbered passages; none when the best is below it, since then the documents say too little about the request. a weaker passage is left out so no draft rests on it."""
    results = [r for r in results if r["score"] >= floor and r["content"].strip()]
    return [
        Passage(
            i,
            r["chunk_id"],
            r["document_id"],
            r.get("title") or "",
            (r.get("page_start") or 1) if r.get("source", "pdf") in ("pdf", "vision") else None,
            r["content"],
            r.get("section_path"),
            r.get("source", "pdf"),
        )
        for i, r in enumerate(results, start=1)
    ]


def not_run(n: int) -> Review:
    return Review(status="not_reviewed", note="The review could not run.", round=n)


def held(draft: Generated, review: Review) -> Generated:
    """a draft kept without a passing review, marked `review`; one whose key gives itself away is flagged by our own check instead, whether or not a reviewer saw it."""
    if checks.long_key(draft.question):
        review = Review(status="flagged", note=checks.LONG_KEY_NOTE, round=review.round, source="check")
    return draft.model_copy(update={"review": review})


def omitted(n: int) -> Review:
    return Review(status="not_reviewed", note="The reviewer gave no verdict on this draft.", round=n)


def practised(draft: Generated, topic_id: UUID | None) -> PracticeQuestion:
    """a passed draft as the student gets it: the question, its key, explanation and sources, with no marks or marking points."""
    q = draft.question
    return PracticeQuestion(
        prompt=q.prompt,
        type=cast(Practicable, q.type),  # practice asks for no other type
        options=q.options,
        answer=q.answer,
        accepted_answers=q.accepted_answers,
        explanation=draft.explanation,
        evidence=draft.evidence,
        sources=q.sources,
        topic_id=topic_id,
    )


def too_little() -> HTTPException:
    return failure(
        422,
        "INSUFFICIENT_SOURCE",
        "The chosen documents say too little about this. Choose other documents or change the focus.",
    )


def unchecked() -> HTTPException:
    """practice whose drafts the reviewer never judged, so none may be served (adr 0022)."""
    return failure(
        503,
        "REVIEW_UNAVAILABLE",
        "Your practice questions could not be checked this time. Try again in a moment.",
    )


@dataclass(frozen=True)
class Audience:
    """who a generation is for: its instructions and call names, whose allowance pays, and whether drafts without a passing review may be served."""

    generation: str
    review: str
    generated: str
    reviewed: str
    payer: str
    serves_unpassed: bool
    rejected: str  # the message when no draft is left


# the teacher reviews every draft (adr 0006); practice reaches the student unseen, so only passed drafts do (adr 0022).
FOR_TEACHER = Audience(
    prompts.GENERATION_INSTRUCTIONS,
    prompts.REVIEW_INSTRUCTIONS,
    "questions.generated",
    "questions.reviewed",
    TEACHER,
    True,
    "None of the generated questions passed the checks. Try another focus or other documents.",
)
FOR_STUDENT = Audience(
    prompts.PRACTICE_GENERATION_INSTRUCTIONS,
    prompts.PRACTICE_REVIEW_INSTRUCTIONS,
    "practice.generated",
    "practice.reviewed",
    STUDENT,
    False,
    "None of the practice questions passed the checks. Try other materials or a different focus.",
)


@dataclass(frozen=True)
class Job:
    """what every round of one generation request shares."""

    topic: str
    body: GenerateRequest
    passages: list[Passage]
    deadline: float  # every call after the first generation is cut off here
    with_review: bool


@dataclass
class Run:
    """what a reviewed generation has so far."""

    accepted: list[Generated] = field(default_factory=list)  # passed the checks and the review
    unreviewed: list[Generated] = field(default_factory=list)  # passed the checks; the review did not run
    # flagged drafts with their reason; kept if nothing better turns up
    weak: list[tuple[Generated, str]] = field(default_factory=list)
    # every refused question, fed back to the generator
    attempts: list[checks.Rejected] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)
    rounds: list[Round] = field(default_factory=list)
    calls: int = 0
    stop_error: UpstreamError | None = None
    unreadable_retry: bool = False
    audience: Audience = FOR_TEACHER

    @property
    def held(self) -> list[Generated]:
        """the drafts that may be served."""
        # practice serves no unreviewed draft, so one neither fills its type nor blocks a rewrite of it (adr 0022).
        return self.accepted + self.unreviewed if self.audience.serves_unpassed else self.accepted

    def shortfall(self, body: GenerateRequest) -> dict[str, int]:
        """how many more of each type are still wanted, leaving out the types that are full."""
        have = Counter(g.question.type for g in self.held)
        return {t: body.counts[t] - have[t] for t in body.question_types if body.counts[t] > have[t]}

    def next_request(self, body: GenerateRequest, shortfall: dict[str, int]) -> GenerateRequest:
        """the next round's request: the shortfall, with the drafts held as existing; weak and rejected drafts go back only as previous_attempts, so a corrected rewrite is not a duplicate."""
        held = [g.question.prompt for g in self.held]
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
                self.unreviewed.append(held(draft, omitted(n)))
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
        if not self.audience.serves_unpassed:
            return self.passed_only(body)
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

    def passed_only(self, body: GenerateRequest) -> list[Generated]:
        """a student's practice: the passed drafts, at most the count of each type; flagged and unreviewed ones are counted as dropped, since no teacher reviews them (adr 0022)."""
        self.dropped.update(reason for _, reason in self.weak)
        if self.unreviewed:
            self.dropped[review.NOT_REVIEWED] += len(self.unreviewed)
        return [
            g
            for t in body.question_types
            for g in [a for a in self.accepted if a.question.type == t][: body.counts[t]]
        ]


class Pipeline:
    def __init__(self, services: Services, request: Request, claims: Claims, settings: Settings) -> None:
        self.services, self.request, self.claims, self.settings = services, request, claims, settings
        self.authorization = request.headers["authorization"]
        self.correlation: str = request.state.correlation_id

    async def generate(self, body: GenerateRequest) -> GenerateResponse:
        started = time.monotonic()
        classroom = str(body.classroom_id)
        await require_relation(
            self.services.identity, self.request, classroom, allowed=(RELATION_TEACHER,), denied=NOT_TEACHER
        )
        try:
            topic, query = await self._topic(
                body.topic_id, body.focus, "Choose a topic from your curriculum."
            )
            passages = await self._passages(classroom, query[:MAX_QUERY], body, GENERATION_PASSAGES)
        except UpstreamError as e:
            log.warning("generation_failed", extra={"error": str(e)})
            raise refused(e) from None
        with_review = self.settings.review and body.review
        final, run = await self._rounds(topic, body, passages, started, with_review, FOR_TEACHER)
        return GenerateResponse(
            questions=final,
            requested=body.count,
            requested_by_type={t: n for t, n in body.counts.items()},
            returned=len(final),
            dropped=dict(run.dropped),
            rounds=run.rounds,
            model_calls=run.calls,
        )

    async def practice(self, body: PracticeRequest) -> PracticeResponse:
        """a student's own practice (adr 0022): the teacher's pipeline, billed to the student, with the review always on and only passed drafts kept."""
        started = time.monotonic()
        classroom = str(body.classroom_id)
        await require_relation(
            self.services.identity, self.request, classroom, allowed=(RELATION_STUDENT,), denied=NOT_STUDENT
        )
        if not self.settings.review:
            # no teacher reviews practice, so it is never served unreviewed.
            raise failure(
                503, "AI_UNAVAILABLE", "Practice questions are not available right now. Try again later."
            )
        try:
            topic, query = await self._practice_query(classroom, body)
            passages = await self._passages(classroom, query[:MAX_QUERY], body, GENERATION_PASSAGES)
        except UpstreamError as e:
            log.warning("practice_failed", extra={"error": str(e)})
            raise refused(e) from None
        request = GenerateRequest(**body.model_dump(exclude={"topic_id"}), topic_id=body.topic_id or NO_TOPIC)
        final, run = await self._rounds(topic, request, passages, started, True, FOR_STUDENT)
        return PracticeResponse(
            questions=[practised(g, body.topic_id) for g in final],
            requested=request.count,
            returned=len(final),
            dropped=dict(run.dropped),
            model_calls=run.calls,
        )

    async def _practice_query(self, classroom: str, body: PracticeRequest) -> tuple[str, str]:
        """the topic's name ("" without one) and what to search for: the topic and focus, the focus alone, or else the chosen documents' titles."""
        if body.topic_id is not None:
            return await self._topic(
                body.topic_id, body.focus, "Choose a topic from your class's curriculum."
            )
        if body.focus:
            return "", body.focus
        documents = [str(d) for d in body.document_ids]
        titles = await self.services.document_titles(
            self.authorization, classroom, documents, self.correlation
        )
        if not titles:
            raise too_little()  # none of them is a document the student can see here
        return "", ". ".join(titles)

    async def _topic(self, topic_id: UUID, focus: str, unknown: str) -> tuple[str, str]:
        """the curriculum topic's name, and the search query: the topic, with the focus when there is one."""
        topic = await self.services.topic(self.authorization, str(topic_id), self.correlation)
        if topic is None:
            raise failure(422, "UNKNOWN_TOPIC", unknown)
        return topic.name, f"{topic.name}. {focus}" if focus else topic.name

    async def _ask(
        self,
        messages: list[dict[str, str]],
        tokens: int,
        schema: dict[str, Any],
        name: str,
        deadline: float | None,
        run: Run,
    ) -> str:
        """one model call, paid from the audience's allowance; with a deadline it is cut off then (OUT_OF_TIME, not a failure to report, only the end of the retries), so the request ends before the gateway gives up."""
        run.calls += 1
        payer = billed_to(self.claims, run.audience.payer)
        return await self.services.generate(messages, tokens, schema, name, self.correlation, payer, deadline)

    async def _rounds(
        self,
        topic: str,
        body: GenerateRequest,
        passages: list[Passage],
        started: float,
        with_review: bool,
        audience: Audience,
    ) -> tuple[list[Generated], Run]:
        """generate, review and retry the shortfall (adr 0006); without a review, one round of checks only. a retry needs 1.5 times the longest round to fit in review_deadline, and every call after the first generation is cut off at it. returns the drafts to serve, never none, and the run."""
        run = Run(audience=audience)
        job = Job(topic, body, passages, started + self.settings.review_deadline, with_review)
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
            output = await self._generate(job, n, request, run)
            if output is None or not await self._check_and_review(job, n, output, request, run):
                break
        return self._final(job, run), run

    async def _generate(self, job: Job, n: int, request: GenerateRequest, run: Run) -> str | None:
        """round n's generation call; none when a retry's call failed and the run keeps what it has."""
        try:
            output = await self._ask(
                prompts.generation_messages(
                    job.topic, request, job.passages, run.attempts, run.audience.generation
                ),
                GENERATION_TOKENS,
                prompts.generation_schema(request.question_types),
                run.audience.generated,
                job.deadline if n > 0 else None,
                run,
            )
        except UpstreamError as e:
            log.warning("generation_failed", extra={"error": str(e), "round": n})
            if n == 0:
                raise refused(e) from None
            run.stop_error = None if e.code == OUT_OF_TIME else e
            return None
        return output

    async def _check_and_review(
        self, job: Job, n: int, output: str, request: GenerateRequest, run: Run
    ) -> bool:
        """checks and reviews round n's reply and records the round; false when the run stops here."""
        try:
            kept, dropped, rejected, generated = checks.questions(output, request, job.passages)
        except checks.UnreadableOutputError:
            if n == 0:
                raise unusable_reply() from None
            run.rounds.append(
                Round(generated=0, passed_checks=0, passed_review=0, rejected={UNREADABLE_REPLY: 1})
            )
            run.unreadable_retry = True
            return False
        counts, sent_back = dropped, Counter[str]()
        run.checked(rejected, dropped, sent_back)
        if not kept:
            run.rounds.append(_round(generated, kept, counts, sent_back, 0))
            return True
        if not job.with_review:
            run.unreviewed += [held(g, g.review) for g in kept]
            run.rounds.append(_round(generated, kept, counts, sent_back, len(kept)))
            return True
        try:
            verdicts = await self._review(job, kept, run)
        except (UpstreamError, checks.UnreadableOutputError) as e:
            log.warning("review_failed", extra={"error": str(e), "round": n})
            run.unreviewed += [held(g, not_run(n)) for g in kept]
            run.rounds.append(_round(generated, kept, counts, sent_back, len(kept)))
            run.stop_error = e if isinstance(e, UpstreamError) and e.code != OUT_OF_TIME else None
            return False
        passed = run.judged(n, kept, verdicts, counts, sent_back)
        run.rounds.append(_round(generated, kept, counts, sent_back, passed, reviewed=True))
        return True

    def _final(self, job: Job, run: Run) -> list[Generated]:
        """the drafts to serve; a run that ends with none is refused."""
        body = job.body
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
            if run.unreviewed and not run.audience.serves_unpassed:
                # the reviewer ran out of time, could not be read or gave no verdict: nothing was refused.
                raise unchecked()
            raise failure(422, "GENERATION_REJECTED", run.audience.rejected)
        return final

    async def _review(self, job: Job, kept: list[Generated], run: Run) -> list[review.Verdict]:
        """one reviewer call over the drafts that passed the checks; it sees the editor's questions and those held so far."""
        output = await self._ask(
            prompts.review_messages(
                job.topic,
                job.body.difficulty,
                kept,
                job.passages,
                [*job.body.existing_prompts, *(g.question.prompt for g in run.held)],
                run.audience.review,
            ),
            REVIEW_TOKENS,
            prompts.review_schema(),
            run.audience.reviewed,
            job.deadline,
            run,
        )
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
        self,
        classroom: str,
        query: str,
        body: GenerateRequest | GuidanceRequest | PracticeRequest,
        top_k: int,
    ) -> list[Passage]:
        documents = [str(d) for d in body.document_ids]
        results, fallback = await self.services.search(
            self.authorization, classroom, query, documents, top_k, self.correlation
        )
        if fallback and results:
            # without the reranker the results are in rrf order and the floor can't be applied, so the
            # drafts would rest on passages nobody scored.
            raise UpstreamError("content", 503, "RERANK_UNAVAILABLE")
        passages = passages_of(results, self.settings.relevance_floor)
        if not passages:
            raise too_little()
        return prompts.fitting(passages)
