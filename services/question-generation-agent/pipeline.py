"""question generation and marking guidance (adr 0006), with a reviewer pass that sends rejected drafts back for a bounded number of rewrites (adr 0015), and a student's own practice on the same loop (adr 0022); the reviewer runs on the check profile and also reviews guidance (adr 0026)."""

import logging
import time
from collections import Counter
from collections.abc import Callable
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
from learning_transport.agents import (
    OUT_OF_TIME,
    CheckFailedError,
    ai_core_refusal,
    billed_to,
    reask_messages,
    unusable_reply,
)

import checks
import prompts
import review
from clients import Services, billed, counted
from config import MAX_REVIEW_RETRIES, Settings
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
GUIDANCE_REVIEW_TOKENS = 1000
# a request's ai-core requests, re-asks included: three writes and three reviews (adr 0015, 0022). a write's
# re-ask is only asked while it and the round's review still fit, so a re-ask costs a later round, not a 7th call.
MAX_CALLS = 2 * (MAX_REVIEW_RETRIES + 1)
WEAK_DISTRACTORS = "WEAK_DISTRACTORS"
UNREADABLE_REPLY = "UNREADABLE_REPLY"
NOT_TEACHER = ("FORBIDDEN", "Only the classroom's teacher can generate questions or marking guidance.")
NOT_STUDENT = ("FORBIDDEN", "Only a student enrolled in this classroom can generate practice questions.")
GUIDANCE_REJECTED = "The drafted guidance did not pass the checks. Try again."
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


def weak_of(draft: Generated, verdict: review.Verdict) -> tuple[str, ...]:
    """the draft's wrong options the reviewer named as weak, as the draft wrote them; a name that is not one of them is left out."""
    q = draft.question
    wrong = {
        o.strip().casefold(): o for o in q.options if o.strip().casefold() != q.answer.strip().casefold()
    }
    named = (wrong.get(o.casefold()) for o in verdict.weak_options)
    return tuple(dict.fromkeys(o for o in named if o is not None))


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
    # a teacher's weak-distractor drafts whose key the reviewer verified: also in `weak`, served flagged
    # and counted toward their type, so no round is paid to rewrite them
    settled: list[Generated] = field(default_factory=list)
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
        """the drafts that may be served and fill their type."""
        # practice serves no unreviewed or flagged draft, so one neither fills its type nor blocks a rewrite of
        # it (adr 0022).
        if not self.audience.serves_unpassed:
            return self.accepted
        return self.accepted + self.unreviewed + self.settled

    def shortfall(self, body: GenerateRequest) -> dict[str, int]:
        """how many more of each type are still wanted, leaving out the types that are full."""
        have = Counter(g.question.type for g in self.held)
        return {t: body.counts[t] - have[t] for t in body.question_types if body.counts[t] > have[t]}

    def next_request(self, body: GenerateRequest, shortfall: dict[str, int]) -> GenerateRequest:
        """the next round's request: the shortfall, with the drafts held as existing; the other weak and rejected drafts go back only as previous_attempts, so a corrected rewrite is not a duplicate."""
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
            if reason in (WEAK_DISTRACTORS, checks.LONG_KEY):
                flagged = Review(
                    status="flagged", note=note, round=n, source="check" if long_key else "reviewer"
                )
                self.weak.append((draft.model_copy(update={"review": flagged}), reason))
                if reason == WEAK_DISTRACTORS and verdict.key_holds and self.audience.serves_unpassed:
                    # the teacher reviews it anyway, and its key is right: served flagged, not rewritten.
                    self.settled.append(self.weak[-1][0])
                    continue
            else:
                self.dropped[verdict.reason] += 1
            problem = "" if long_key else verdict.problem
            self.attempts.append(
                checks.Rejected(
                    draft.question.prompt, draft.question.type, reason, problem, weak_of(draft, verdict)
                )
            )
            sent_back[reason] += 1
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

    async def _ask(  # noqa: PLR0913 - one call, its cut-off, profile and re-ask
        self,
        messages: list[dict[str, str]],
        tokens: int,
        schema: dict[str, Any],
        name: str,
        deadline: float | None,
        run: Run,
        *,
        profile: str = "default",
        effort: str | None = None,
        check: Callable[[str], list[str]] | None = None,
        reask: int = 0,
    ) -> str:
        """one model call and its re-asks, paid from the audience's allowance and counted in run.calls; with a deadline it is cut off then (OUT_OF_TIME, not a failure to report, only the end of the retries), so the request ends before the gateway gives up."""
        payer = billed_to(self.claims, run.audience.payer)
        with counted() as calls:
            try:
                return await self.services.generate(
                    messages, tokens, schema, name, self.correlation, payer, deadline,
                    profile=profile, reasoning_effort=effort, check=check, reask=reask,
                )  # fmt: skip
            finally:
                # one refused before any request still counts as attempted (adr 0015).
                run.calls += max(calls[0], 1)

    def _first_deadline(self, deadline: float) -> float:
        """a request's first write is never cut off before its own timeout, as before re-asks; its re-ask must start inside that too. a moment past the timeout, so a slow reply is still ai-core's timeout, not OUT_OF_TIME."""
        return max(deadline, time.monotonic() + self.services.generate_timeout + 1)

    async def _rounds(
        self,
        topic: str,
        body: GenerateRequest,
        passages: list[Passage],
        started: float,
        with_review: bool,
        audience: Audience,
    ) -> tuple[list[Generated], Run]:
        """generate, review and retry the shortfall (adr 0006); without a review, one round of checks only. a retry needs 1.5 times the longest round to fit in review_deadline and its write and review to fit in MAX_CALLS; every call is cut off at the deadline. returns the drafts to serve, never none, and the run."""
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
                if run.calls + 1 + with_review > MAX_CALLS:
                    break  # a re-ask used this round's calls
                run.rounds[-1].retried = True
            round_started = now
            request = run.next_request(body, shortfall)
            output = await self._generate(job, n, request, run)
            if output is None or not await self._check_and_review(job, n, output, request, run):
                break
        return self._final(job, run), run

    async def _generate(self, job: Job, n: int, request: GenerateRequest, run: Run) -> str | None:
        """round n's generation call, asked once more when its reply is refused by ai-core or cannot be read and the re-ask and the round's review still fit in MAX_CALLS; none when a retry's call failed and the run keeps what it has."""
        reask = int(run.calls + 2 + job.with_review <= MAX_CALLS)
        deadline = job.deadline if n > 0 else self._first_deadline(job.deadline)
        try:
            output = await self._ask(
                prompts.generation_messages(
                    job.topic, request, job.passages, run.attempts, run.audience.generation
                ),
                GENERATION_TOKENS,
                prompts.generation_schema(request.question_types),
                run.audience.generated,
                deadline,
                run,
                check=lambda out: checks.reply_faults(out, "questions"),
                reask=reask,
            )
        except CheckFailedError:
            # still unreadable after the re-ask: as an unreadable reply always was.
            if n == 0:
                raise unusable_reply() from None
            run.rounds.append(
                Round(generated=0, passed_checks=0, passed_review=0, rejected={UNREADABLE_REPLY: 1})
            )
            run.unreadable_retry = True
            return None
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
                "prompt_version": prompts.PROMPT_VERSION,
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
            profile="check",  # a second opinion, not the writer's settings (adr 0026)
            effort=self.settings.review_effort,
        )
        return review.verdicts(output, len(kept), [d.question.type == "multiple_choice" for d in kept])

    async def guidance(self, body: GuidanceRequest) -> Guidance:
        """drafted marking guidance (adr 0006): written, asked once more when it fails the checks, then reviewed on the check profile; a failed review gets the one re-ask if it is unused, and the rewrite is reviewed again (adr 0026). at most four calls: write, review, rewrite, review; or write, re-ask, review."""
        started = time.monotonic()
        classroom = str(body.classroom_id)
        await require_relation(
            self.services.identity, self.request, classroom, allowed=(RELATION_TEACHER,), denied=NOT_TEACHER
        )
        deadline = started + self.settings.review_deadline
        calls = [0]
        try:
            passages = await self._passages(classroom, body.prompt[:MAX_QUERY], body, GUIDANCE_PASSAGES)
            messages = prompts.guidance_messages(body.prompt, body.type, body.points, passages)
            first = self._first_deadline(deadline)
            output, guidance = await self._guidance_draft(messages, body, passages, first, 1, calls)
            reasked = calls[0] > 1
            if not self.settings.review:
                return guidance
            verdict = await self._guidance_review(body, guidance, passages, deadline, calls)
            if verdict is None or verdict.passed:
                return guidance
            if reasked:
                raise self._guidance_rejected(verdict.reason, calls)  # the re-ask is used up
            finding = verdict.problem or prompts.PROBLEMS.get(verdict.reason, verdict.reason)
            faults = [f"a reviewer found that {finding}" + (f" ({verdict.note})" if verdict.note else "")]
            again = reask_messages(messages, output, faults)
            _, guidance = await self._guidance_draft(again, body, passages, deadline, 0, calls)
            verdict = await self._guidance_review(
                body, guidance, passages, deadline, calls, after_rewrite=True
            )
            if verdict is not None and not verdict.passed:
                raise self._guidance_rejected(verdict.reason, calls)
            return guidance
        except UpstreamError as e:
            log.warning("guidance_failed", extra={"error": str(e), "calls": calls[0]})
            raise refused(e) from None

    async def _guidance_draft(
        self,
        messages: list[dict[str, str]],
        body: GuidanceRequest,
        passages: list[Passage],
        deadline: float,
        reask: int,
        calls: list[int],
    ) -> tuple[str, Guidance]:
        """the drafted guidance and its reply, through the checks; refused as unusable or rejected when it still fails them."""

        def faults(output: str) -> list[str]:
            try:
                checks.guidance(output, body, passages)
            except checks.UnreadableOutputError:
                return [checks.UNREADABLE]
            except checks.RuleBrokenError as d:
                return [prompts.PROBLEMS.get(d.reason, d.reason)]
            return []

        with counted() as made:
            try:
                output = await self.services.generate(
                    messages, GUIDANCE_TOKENS, prompts.guidance_schema(), "marking.guidance",
                    self.correlation, billed(self.claims), deadline, check=faults, reask=reask,
                )  # fmt: skip
            except CheckFailedError as e:
                if e.errors == [checks.UNREADABLE]:
                    raise unusable_reply() from None
                raise self._guidance_rejected(e.errors[0], calls) from None
            finally:
                calls[0] += max(made[0], 1)
        return output, checks.guidance(output, body, passages)

    async def _guidance_review(
        self,
        body: GuidanceRequest,
        guidance: Guidance,
        passages: list[Passage],
        deadline: float,
        calls: list[int],
        after_rewrite: bool = False,
    ) -> review.Verdict | None:
        """the reviewer's verdict on the guidance; none when it could not run, and the teacher gets the guidance to review as before, unless it is a rewrite of guidance the reviewer failed, which is not served unreviewed."""
        with counted() as made:
            try:
                output = await self.services.generate(
                    prompts.guidance_review_messages(body.prompt, guidance, body.points, passages),
                    GUIDANCE_REVIEW_TOKENS, prompts.guidance_review_schema(), "marking.guidance.reviewed",
                    self.correlation, billed(self.claims), deadline, profile="check",
                    reasoning_effort=self.settings.review_effort,
                )  # fmt: skip
                return review.guidance_verdict(output)
            except (UpstreamError, checks.UnreadableOutputError) as e:
                log.warning("guidance_review_failed", extra={"error": str(e)})
                if not after_rewrite:
                    return None
                if isinstance(e, UpstreamError) and e.code != OUT_OF_TIME:
                    raise
                raise self._guidance_rejected("NOT_REVIEWED", calls) from None
            finally:
                calls[0] += max(made[0], 1)

    @staticmethod
    def _guidance_rejected(reason: str, calls: list[int]) -> HTTPException:
        log.info(
            "guidance rejected",
            extra={"reason": reason, "calls": calls[0], "prompt_version": prompts.PROMPT_VERSION},
        )
        return failure(422, "GUIDANCE_REJECTED", GUIDANCE_REJECTED)

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
