# ADR 0011: Users can delete their own account

Status: accepted 2026-10-18. The project owner asked for account deletion through a simple Profile tab.

## Context

ADR 0010 left account deletion open: nothing erased a user, and doing so needed an event every data-holding service consumes and a decision about ai-core's usage ledger. Identity's `users` row cannot be removed, because a deleted classroom keeps its `teacher_id`.

## Decision

**Identity deletes the account.** `DELETE /v1/me` (the signed-in user, 204; 404 when already deleted) runs in one transaction: it locks the user's row, refuses a teacher who still owns a live classroom with `409 OWNS_CLASSROOMS`, deletes the user's enrollments, sets `users.deleted_at` and clears `username` and `display_name` (migration `007_deleted_accounts.sql`, additive), and writes `identity.user.deleted.v1` (`{user_id, org_id, role}`) to the outbox. A deleted account cannot register again (`403 ACCOUNT_DELETED`), be enrolled (`422 INVALID_STUDENT`) or create a classroom, and leaves student lookups and lists.

**A teacher deletes their classrooms first.** Deleting a classroom already purges its materials, assessments, submissions, grades, evidence and chat threads in every service, so the account deletion does not repeat that, and a teacher cannot delete their students' work by deleting themselves without the classroom dialog's warning.

**Each service purges its own data.** On `identity.user.deleted.v1`, in the consumer's transaction and under an exclusive `user` lock that their inserts take shared:
- Assessment (group `assessment-users-v1`, migration `005_deleted_users.sql`) deletes the student's saved answers, submissions and the submission events still in its outbox, and refuses further saves and submissions (`403`).
- Grading (group `grading-users-v1`, migration `007_deleted_users.sql`) deletes the student's evaluations, audit, marking jobs, removal records and outbox events, and never grades a late submission event for them.
- Analytics (migration `006_deleted_users.sql`) deletes the student's evidence and evaluations and ignores later grades for them.
- The chatbot (migration `004_deleted_users.sql`) deletes the user's chat threads and refuses new ones.

Each keeps `deleted_users`, so a late or replayed event cannot bring data back. A duplicate deletion changes nothing.

**The sign-in goes too.** Identity removes the user from Keycloak every 15 seconds for each deleted account not yet removed (`signin_removed_at`), with a confidential `identity-accounts` client that holds only `realm-management/manage-users`. A user already missing counts as removed. A token issued before the deletion stays valid until it expires; the web app signs the user out at once.

**ai-core's usage ledger is kept.** Its cost records are the billing record of who spent what, per user (ADR 0003); they hold no answers or content.

**The web app has a Profile tab** for both roles: the account's name, username, role, organization and ID, and a Delete account button behind the app's confirmation dialog with Cancel focused. A teacher who still has classrooms sees how many and that they must delete them first, with the button disabled; unsaved work elsewhere blocks the deletion.

## Consequences

- A local setup runs `python scripts/upgrade_local.py` (it adds `IDENTITY_ACCOUNTS_CLIENT_SECRET` to `.env` and creates the Keycloak client in an existing realm), then `docker compose up -d --build`, so `kafka-init` creates `identity.user.deleted.v1` and its `.dlq` and the services apply their migrations. Deploy Assessment, Grading, Analytics and the chatbot before or with Identity.
- Copies of events on Kafka remain until retention expires (7 days for submissions and grades, ADR 0010). Content holds no per-user data outside classrooms; ai-core's ledger is kept as above.
- Not changed: an administrator cannot delete another user, and a deletion cannot be undone.
