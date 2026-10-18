# ADR 0008: Deleting assessments students have submitted

Status: accepted 2026-10-18. The project owner asked for teachers to be able to delete an assessment after students have submitted it, together with its grades.

## Context

ADR 0004, as amended on 2026-10-18, lets a teacher delete a published assessment only until a student submits it. After that, deletion is refused with `409 HAS_SUBMISSIONS`, because the submissions and their grades belong to it. A teacher who published by mistake, or wants to retire an old assessment, could not remove it. The assessment's data is spread across services: Assessment holds the submissions, Grading the evaluations and their audit trail, and Analytics the performance evidence. No service may touch another's database.

## Decision

**The teacher confirms deleting the grades as well.** `DELETE /v1/assessments/{id}` still answers `409 HAS_SUBMISSIONS` once anyone has submitted. Repeating it with `?with_submissions=true` deletes the assessment with its submissions and saved work. The flag does nothing for an assessment nobody has submitted. Only the classroom's teacher may delete, as before.

**The owners purge their own data through an event.** When the deleted assessment was published, Assessment writes `assessment.deleted.v1` (`{assessment_id, classroom_id, org_id}`) to its outbox in the delete's transaction, and drops its unsent events for the assessment. Deleting a draft writes nothing, since no other service knows of it.

- Grading deletes the assessment's evaluations, their audit trail and marking jobs, and its unsent events for it, released and finalized grades included.
- Analytics deletes the assessment's evidence and evaluations, so class performance and students' progress stop counting it.
- Each keeps `deleted_assessments` (migration `005_deleted_assessments.sql`, additive), so a submission or finalized grade for the assessment that arrives after the purge is ignored. Grading's purge takes an exclusive assessment lock that its new-evaluation insert takes shared, so a submission being graded during the purge cannot survive it; Analytics uses its classroom lock.
- A repeated event changes nothing; a malformed one is dead-lettered to `assessment.deleted.v1.dlq`.

**The browser says what goes.** The delete dialog for a submitted assessment is titled "Delete this assessment and its grades?", counts the submissions, says finalized and released grades go too, and its confirm button reads "Delete assessment and grades". Cancel is focused first.

## Consequences

- This cannot be undone. Students lose those results from My results, and the Class dashboard and progress views stop counting them.
- An existing local setup needs `docker compose up -d --build`, so `kafka-init` creates the new topics and the services pick up the migrations and consumers.
- Not purged: ai-core's usage records for AI marking suggestions (billing, per user), and copies of events still on Kafka until retention expires. Since ADR 0010 that is 7 days for submissions, finalized grades and dead letters (it was 30), and published outbox events are pruned after 7 days; the delete also removes submission events written as older versions.
