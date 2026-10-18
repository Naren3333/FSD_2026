# ADR 0010: No stale data after a removal or deletion

Status: accepted 2026-10-18. The project owner asked for the findings of the stale-data audit to be fixed.

## Context

An audit of the delete and archive flows found data that outlived the action that should have ended it. Unenrolling a student announced nothing, so their drafts stayed in the teacher's review queue and counts, and AI marking queued for them kept running and billing the teacher. A deleted material's text stayed readable in students' chat citations and kept shaping later answers. Kafka kept every submission (with answer keys) and grade for 30 days, and every outbox kept its published events forever. Assessment's delete missed submission events written as older versions. Extending a due date left `late` flags set. Topic prerequisites could be written but never read or removed. Objects whose row never committed stayed in the bucket.

## Decision

**Removing a student is an event.** Identity writes `identity.enrollment.removed.v1` (`{classroom_id, org_id, student_id}`) to its outbox in the removal's transaction. Grading (group `grading-enrollments-v1`) records the removal in `removed_enrollments` (migration `006_removed_students_and_due_dates.sql`, additive). A marking job for work submitted before the removal finishes with the reason `STUDENT_REMOVED` and makes no model call. The student's submissions and grades are kept, as before.

**Teacher lists follow the roster.** Grading's review list and counts, and Assessment's list and progress counts, read the classroom's roster from Identity with the teacher's token (as Analytics already did) and count only enrolled students. A removed student's work leaves those views and comes back if they are enrolled again.

**A deleted material leaves chat history.** The chatbot consumes `content.deleted.v1` (its existing group, so `content.deleted.v1.dlq` is new). It keeps `deleted_documents` (migration `003_deleted_documents.sql`) and blanks the content and figure of every citation naming the document, marking it `removed: true`, under a document lock that saving an answer also takes, so an answer finishing during the delete is redacted too. Redacted citations are no longer fed back as history. The chat shows the source's title with "The teacher deleted this material, so its passage is no longer shown." and no passage, figure or Open the PDF button.

**Answers and grades do not linger.** Kafka keeps the submission, finalized-grade and dead-letter topics for 7 days (`retention.ms=604800000`, set by `kafka-init` with `kafka-configs.sh --alter`, so existing topics change too); other topics keep the broker's 30 days. Each outbox deletes events published more than 7 days ago (`OutboxRetention`), up to 1000 a minute; unpublished and abandoned events are never pruned. Assessment's delete removes submission events of every version (`assessment.submitted.%`).

**A due date change decides lateness again.** When `PATCH` changes a published assessment's due date, Assessment recomputes `submissions.late` from each submission's time and writes `assessment.due_changed.v1` (`{assessment_id, classroom_id, org_id, due_at}`, `due_at` nullable). Grading (group `grading-due-dates-v1`) keeps the newest due date per assessment in `assessment_due` and recomputes `evaluations.late`; a submission graded later takes its lateness from that row. An older or repeated event changes nothing.

**Prerequisites can be read and removed.** `GET /v1/topics/{id}/prerequisites` lists a topic's active prerequisites; `DELETE /v1/topics/{id}/prerequisites/{prerequisite}` (teacher only, 204, or 404) removes one.

**Content sweeps orphaned objects.** Every hour Content removes objects older than 6 hours that no document row names, as its original or a figure a chunk cites, skipping documents still indexing.

## Consequences

- A local setup needs `docker compose up -d --build`, so `kafka-init` creates `identity.enrollment.removed.v1`, `assessment.due_changed.v1` and their `.dlq` topics plus `content.deleted.v1.dlq`, and sets the 7-day retention; Grading and the chatbot apply their migrations. Deploy Grading and the chatbot before or with Identity, Assessment and Content.
- An Analytics rebuild from Kafka (operations.md) now reaches back 7 days, not 30.
- Removals made before this change were never announced. Their students' work leaves teacher lists at once, since lists follow the roster, but marking jobs already queued for them are not stopped.
- Not changed: there is still no account deletion (since added by ADR 0011). Test-only topic cleanup (`scripts/local_stack.py`) still deletes topics directly.
- Amended 2026-10-18: a release applies only to currently enrolled students; removed students' finalized grades stay unreleased (and kept) until they are enrolled again.
