# ADR 0020: Marking critic

Status: accepted 2026-11-21. The project owner asked for the second agentic workflow (the list is in `docs/architecture/agentic-workflows.md`). It amends ADR 0004.

## Context

ADR 0004's marker makes one model call per written answer and stores the suggestion with the model's own `confidence`. Nothing checks it, so a confidently wrong suggestion looks the same to the teacher as a sound one. ADR 0004 listed a second AI opinion as not built.

## Decision

**A second, blind marking checks every ready suggestion inside Grading, and code compares the two.**

- When a suggestion is `ready`, its `marking_jobs` row becomes a review job in the same transaction (`stage` `review`, migration `011_marking_review.sql`, additive), with fresh attempts, and the suggestion is stored with `review: {status: "pending"}`. Failed suggestions are not reviewed.
- The critic is a second ai-core `generate` call (`schema_name` `marking.review`), billed to the same teacher through the ADR 0003 spend check. It gets the question, rubric, model answer, marking points and answer exactly as the marker does, under its own system prompt (an independent second examiner who marks strictly and, when unsure, chooses the lower level and says low confidence). It never sees the first suggestion. Its reply uses the marker's schema and passes the same checks (rubric levels, quotes in the answer, nothing quoted against a level above the lowest).
- `compareSuggestions` sets the review to `doubtful` when any criterion's level differs (`LEVELS_DIFFER`), either marking says low confidence (`LOW_CONFIDENCE`), or the critic raised a known flag the marker did not (`CRITIC_FLAGGED`), and to `agreed` otherwise. The review keeps the critic's levels with their rationales, its total, confidence, model and new flags; its feedback to the student is dropped.
- It fails closed: a reply that fails the checks, a failed call or exhausted attempts make the review `not_reviewed` with the reason, never `agreed`. Waiting follows ADR 0004 (spend limit, `AI_BUSY` and an unconfigured provider wait without using an attempt; provider failures back off; a rejected reply is not retried), with the reason on the pending review.
- The review is merged into the suggestion only while the evaluation is a draft and the suggestion is still the ready one that was reviewed; finalizing deletes the job and turns a review still pending into `not_reviewed` (`FINALIZED`), so a review finished afterwards is dropped and a reopened grade never waits on a check that will not run. A review stopped because the student was removed (`STUDENT_REMOVED`) is queued again when they return. A teacher who saved the suggestion's own marks (the same levels, or the same total without criteria) still has them checked, since that is when a wrong suggestion costs most; one who marked the question otherwise first gets `MARKED_BY_TEACHER` without a model call. A suggestion whose evaluation has no teacher to bill ends `NO_TEACHER`.
- The review never changes the suggestion or a mark, and adds no audit entry of its own; a finalize entry records a pending review becoming `not_reviewed` (`FINALIZED`), since it is part of the evidence finalized. Students never receive it (`Evaluation.own()` strips suggestions). The teacher still finalizes.
- `MARKING_CRITIC` on Grading turns it off for `off`, `false`, `0` and `no` (any case, spaces ignored); anything else, or unset, is on. Off, ready suggestions end their job with no review; a review job queued before it was turned off ends `not_reviewed` with `CRITIC_OFF`, without a model call.
- `/v1/evaluations/counts` adds `ready_doubtful`: the drafts a bulk finalize would finalize that have a doubtful suggestion.

**The review page** shows the second check under each ready suggestion ("Second check agrees.", "Second check disagrees: Reason 3 vs 0.", "Second check running.", "Not second-checked: …"). A doubtful one has a "Check AI mark" chip and lists the criteria marked differently with the critic's level and rationale, labelled AI-written. Drafts with a doubtful suggestion carry the chip in the queue and come first on the page, otherwise in the same order; Next to mark follows that order. The order is set when the page loads (a new page, assessment, classroom or Refresh) and kept while it is shown, so rows do not move as second checks finish or grades are finalized. The grade keeps being re-read while a review is pending. Both finalize dialogs warn, without blocking, how many questions or grades have a doubtful suggestion the marks still follow: a doubt stops counting, there and in `ready_doubtful` and the queue's chip, once the teacher's mark for that question gives another total than the suggestion's; Cancel stays first and focused.

## Consequences

- Every written answer with a ready suggestion now costs two model calls instead of one, against the same teacher's daily allowance.
- Real-key run: not run. Fake-model unit and database tests cover the comparison, the blind prompt, the stages and the races; how often the critic disagrees on real answers is not measured yet.
- Upgrade: `docker compose up -d --build`; Grading applies migration 011 on start. No `.env` change is needed. A Grading built before this change does not know the `stage` column and would run a review job as a new suggestion, so stop every old Grading before the new one starts rather than running both at once.
- Not built: checking that similar answers in one assessment got consistent suggestions, measuring how often teachers agree with suggestions or reviews, and a cheaper model for the critic.
