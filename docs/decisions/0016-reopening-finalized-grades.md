# ADR 0016: Reopening finalized grades and finalizing an assessment's marked grades together

Status: accepted 2026-10-26. The project owner asked for the code review's findings to be fixed, including "finalized grades can never be corrected" and "each finalize needs a typed reason".

## Context

A finalized evaluation was permanent: marking and finalizing refused anything but a draft, and `grading.finalized.v3` went out once. Analytics stored the first evidence for an evaluation and ignored any later event for it, so even a corrected grade could never have reached it. A teacher who finalized a wrong mark had no way to fix it.

Every finalize also needed a typed reason, one grade at a time. A fully auto-marked quiz for 30 students took 30 dialogs and 30 reasons for grades the teacher had nothing to change in.

## Decision

**A finalized grade can be reopened.** `POST /v1/evaluations/{id}/reopen` with a required `reason` (1–1000 characters), for the classroom's teacher while the student is on the roster (409 `STUDENT_REMOVED` otherwise, as for marking and finalizing). A draft gets 409 `NOT_FINALIZED`. In one transaction the evaluation becomes a draft again (`finalized_by`, `finalized_at` and `released_at` cleared), its `revision` goes up by one (Grading migration `008_revisions.sql`, additive, existing grades are revision 1) and the audit trail gets an entry with `action='reopen'` and the reason. The evidence and the teacher's marks and feedback are kept, so the teacher corrects what was wrong and finalizes again through the usual finalize, which releases it again under the assessment's release policy (at once with `on_finalize`, at the next release with `manual`). No AI suggestion is asked for again. While it is a draft the student does not see it: their results only list finalized, released grades.

**Analytics follows the newest revision.** `grading.finalized.v3` gains an optional `revision` (an event without one is revision 1, as every grade before this change was). When a reopened grade had been released, Grading writes `grading.reopened.v1` (`{evaluation_id, assessment_id, classroom_id, org_id, student_id, revision}`, the new draft's revision) to its outbox in the reopen's transaction; a grade that was never released was never in Analytics and needs no event. Analytics (migration `007_revisions.sql`, additive) keeps each evaluation's newest revision and whether it is withdrawn:

- a finalized event older than the stored revision changes nothing; one at the stored revision changes nothing unless that revision was withdrawn (a reopen of it got there first); a newer one replaces the evaluation's evidence;
- a reopened event at or below the stored revision changes nothing; a newer one removes the evidence and marks the evaluation withdrawn at that revision.

So Analytics ends with exactly the newest released evidence, whatever order the two topics arrive in. Both topics are keyed by evaluation id, so each keeps its own order, but Kafka does not order one topic against another; revisions do not depend on it. The inbox still drops a repeated event id, and a replay from the start of both topics (a rebuild under a new `KAFKA_GROUP`) reaches the same state. A withdrawn evaluation keeps only its id, the event id and the revision; purges skip it as before, and their remembered classrooms, assessments and users keep refusing later events for it.

**An assessment's fully marked drafts can be finalized together.** `POST /v1/evaluations/finalize` with `{assessment_id, reason?}`, for the classroom's teacher, finalizes in one transaction every draft of the assessment whose student is on the current roster and none of whose questions still needs marking, each with its own audit entry. The reason defaults to "Finalized with the assessment's other fully marked grades". Grades released by `on_finalize` publish `grading.finalized.v3` exactly as a single finalize does. The answer says how many it finalized and how many drafts still need marking. `GET /v1/evaluations/counts` adds `ready`, the drafts that could be finalized this way, so the page can say how many before the teacher confirms.

**The review page offers both.** A finalized grade has "Reopen to correct", which asks for the reason in a confirmation dialog and then opens the grade for marking. With an assessment chosen and drafts ready, "Finalize all fully marked" confirms the count and reloads the page and its counts.

## Consequences

- `kafka-init` creates `grading.reopened.v1` and `grading.reopened.v1.dlq`, with the 7-day retention of the finalized-grade topic. A local setup needs `docker compose up -d --build`.
- Analytics validates events strictly, so an Analytics that predates this change dead-letters a `grading.finalized.v3` carrying `revision`. Deploy Analytics, with its migration, before or with Grading.
- A correction reaches Analytics as a withdrawal and a new finalization: between them, the student's evidence leaves performance views, as the grade leaves their results.
- Not changed: a grade still cannot be edited while finalized; reopening is the way to change it, and every reopen and finalize stays in the audit trail.
