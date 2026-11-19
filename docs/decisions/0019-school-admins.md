# 0019 — School admins manage accounts and classrooms

Status: accepted 2026-11-19 by the project owner, who chose one per-school admin role in place of the platform operator, admin-created accounts, one teacher per classroom and read-only rosters for teachers. Supersedes ADR 0018's operator access and the teacher's classroom management in ADR 0001.

## Context

Before this decision, teachers created and deleted their own classrooms and enrolled students. Accounts existed only in Keycloak, made by local scripts. A separate cross-organization `platform-operator` role could open monitoring only (ADR 0018). Schools want one person to set up the year: create the teacher and student accounts, create the classrooms, give each classroom its teacher and enroll its students. They do not want a role that sees across schools.

## Decision

A new realm role, `admin`, belongs to one school through the account's `org_id`, like every other person. It replaces `platform-operator`, so no role works across organizations. An account holds exactly one of `admin`, `teacher` and `student`. Every backend refuses a token with more than one of them (403 `ROLE_CONFLICT`). Identity's `users.role` accepts `admin` (migration 009, which widens the check without changing any row).

Identity owns the admin operations under `/v1/admin/`, each limited to the admin's own organization. Another school's classroom answers 404.

- **Accounts:** `GET /v1/admin/accounts?role=teacher|student` pages the school's accounts, whether or not they have signed in. `POST /v1/admin/accounts` creates a Keycloak user through the `identity-accounts` client:
  - The user gets the school's `org_id`, the realm role and a random 128-bit temporary password with Keycloak's `UPDATE_PASSWORD` action.
  - Keycloak usernames are unique across the realm, so the sign-in username is the school's `org_id`, a dot, then the username the admin chose, such as `local-school.ada`. A taken username (409 `USERNAME_TAKEN`) is then always one of the admin's own school, and says nothing about other schools. The `org_id` must be 1–64 lowercase letters, digits or hyphens, so its first dot ends it. Identity records and answers the whole sign-in username, which is what its user types.
  - Identity then records the user, so the account can be assigned before its first sign-in. If recording fails, Identity removes the Keycloak user again. Each creation marks its user with a random `identity_request` attribute, so a creation whose answer is lost or is a server error removes only its own account, never one another request made.
  - The password is answered once with `Cache-Control: no-store`. It is never stored or logged.
  - `POST /v1/admin/accounts/{id}/password` gives one of the school's teacher or student accounts a new temporary password, answered the same way. An admin whose creation answer was lost, or whose user forgot their password, resets it; the old password stops working.
  - The client now also holds `view-realm`, which it needs to read the role it assigns. The realm's user profile lets realm admins, not users, edit unmanaged attributes (`ADMIN_EDIT`), so `org_id` is kept and its user cannot change it. Setup and upgrade set `ADMIN_EDIT` over any other policy, including `ENABLED`, which would let users edit it.
- **Classrooms:** `GET`, `POST`, `PATCH` and `DELETE /v1/admin/classrooms[/{id}]`. A classroom still has exactly one teacher, who must be a live teacher of the school. `PATCH` renames the classroom or reassigns its teacher. Every service asks Identity's `/access`, so a reassigned classroom's teacher access moves at once. A new teacher is announced as `identity.classroom.reassigned.v1`, ordered by its timestamp, which is the database's clock read under the classroom's row lock, so reassignments are ordered as they committed, whichever Identity process made them: Grading bills the classroom's AI marking to them from then on, draft grades included, in place of the teacher who created each assessment (ADR 0003). Each teacher's question bank stays personal: the new teacher neither sees nor removes the previous teacher's links to it, which stay with the questions they leave unchanged. Deletion is unchanged and still emits `identity.classroom.deleted.v1`.
- **Enrollment:** `GET /v1/admin/classrooms/{id}/students`, `GET .../student-ids`, `POST .../enrollments` and `DELETE .../enrollments/{student}`. A student is enrolled by account id or by username: the whole sign-in username, or the part the admin chose after the school's id. Removal still emits `identity.enrollment.removed.v1`.

Teachers lose create, delete, enroll, unenroll and the school's student list. They keep their classroom list, `/access`, the roster and `/student-ids`.

Admins get no classroom relation, so no service shows them materials, assessments, grades or chats.

An admin cannot delete their own account (403 `ADMIN_ACCOUNT`), so the `identity.user.deleted.v1` role stays teacher or student. A teacher who still owns classrooms keeps getting 409 `OWNS_CLASSROOMS` and asks an admin to reassign or delete them.

Monitoring requires the `admin` role in place of `platform-operator`. It forces every spend report to the admin's own `org_id` and refuses another organization's (403). ai-core's spend summary gains an `org_id` filter, and a school's summary leaves out the platform-wide ceiling. Service readiness, latency and dead-letter counts are infrastructure with no school data, so they stay as they are.

## Consequences

- Existing environments run `scripts/upgrade_local.py`, which:
  - creates the `admin` role;
  - grants `view-realm` to `identity-accounts`;
  - sets the user profile attribute policy and makes email optional, since created accounts have none;
  - creates the local school's `admin` account from `ADMIN_PASSWORD`.
- `kafka-init` creates `identity.classroom.reassigned.v1` and its `.dlq` topic, and Grading applies the additive `009_classroom_teachers.sql`. Deploy Grading with or before Identity.
- The old `platform-operator` role is left in place, unused.
- The disposable test stack no longer generates a separate operator account.
- Scripts and tests that set up classrooms now do it as the admin.
- Accounts made before this decision, such as the local `teacher` and `student`, keep their usernames. A new account cannot take one of them, since every new username starts with its school's id.
- Keycloak keeps the sign-in accounts. Identity creates them but does not yet disable or rename them. Admin accounts themselves are still provisioned in Keycloak.
