# ADR 0004: Assessment lifecycle, teacher marking and AI marking suggestions

Status: accepted 2026-10-16. The project owner asked for the plan in `plans/assessment-grading-ai-design.md` to be implemented without its agent parts, with the recommended answer to each of its five open decisions.

## Context

Assessments published at once, held one-mark multiple-choice and numerical questions, and showed a grade to the student the moment the teacher finalized it. Teachers could not save a draft, set a due date, ask for written answers or give feedback, and no AI helped with marking. The plan weighed an agent for marking against a fixed pipeline and chose the pipeline: marking is one bounded judgement per answer, so a loop that chooses its own tools would add cost and risk without adding accuracy.

## Decision

**Assessment owns the lifecycle.**

- An assessment is a `draft` or `published`. Drafts can be edited (`PUT`), published (`POST /publish`) or deleted (`DELETE`); students never see one. A published assessment cannot change.
- Optional `opens_at` hides it from students until then. Optional `due_at` must be in the future when it is published.
- `late_policy`: `accept_flagged` (default) takes late work and flags it `late`; `reject` refuses it with `409 CLOSED`.
- `release_policy`: `on_finalize` (default) shows each grade as soon as it is finalized; `manual` waits until the teacher releases the assessment's results.
- `ai_marking` asks for AI suggestions on the questions a teacher marks. The teacher who created the assessment pays for them (`created_by`).
- Students' work in progress is kept in `saved_answers` (`GET`/`PUT /saved-answers`) until they submit.

**Question types.** Each question has 1–100 points.

- Multiple choice and numerical are marked automatically, as before.
- A short answer has 1–20 accepted answers, matched after trimming and collapsing spaces, and without case unless the teacher says case matters. A match earns full marks. Anything else goes to the teacher: it is never marked wrong automatically, since it may be right in other words.
- A free response is marked by the teacher against a rubric of 1–10 criteria, each with 2–6 levels. The question's points are the sum of each criterion's top level. An optional model answer is for the teacher and the AI only.
- Students never receive answer keys, accepted answers, rubrics or model answers.

**Grading stays the owner of grades.**

- `assessment.submitted.v2` carries the points, the new question types, `late`, `submitted_at`, `release_policy`, `ai_marking` and the paying `teacher_id`. Grading still reads v1 events, with one point per question and today's defaults.
- A question the teacher must mark has `needs_marking`. The teacher marks it with `POST /v1/evaluations/{id}/marks`: a level for every rubric criterion, or points for other questions, with optional feedback. Finalizing is refused with `422 NEEDS_MARKING` while any question still needs marking. Finalize also takes the same `marks`, and the older `overrides` still work.
- With `manual` release, students see nothing until `POST /v1/evaluations/release`. A student's own results never include rubric scores or AI suggestions.
- `grading.finalized.v2` carries `awarded` and `possible` for part marks. It is written when the grade is released (at finalization with `on_finalize`, at the release with `manual`), so Analytics never shows a student a grade before release (corrected 2026-10-17; it was first written at finalization). Analytics reads v1 and v2 and adds `marks_awarded` and `marks_possible` to each skill. "Correct" now means full marks.

**AI marking is a suggestion inside Grading, not a service and not an agent.**

- When the assessment asks for it, each question that needs marking gets a `marking_jobs` row in the same transaction as the draft. Grading workers lease jobs with `FOR UPDATE SKIP LOCKED` and make one structured `generate` call to ai-core, as the `grading-marker` service account, billed to the assessment's teacher (ADR 0003).
- The prompt treats the student's answer as data. The reply must use the rubric's own levels, give every criterion once, and back any level above the lowest with quotes that really appear in the answer. Grading adds up the points itself, drops feedback that repeats the model answer, and keeps only known flags. A reply that breaks a rule is stored as a failed suggestion with the reason, never as a mark.
- The spend limit waits for the reset without using an attempt; provider failures retry with backoff up to six attempts. Without provider keys a suggestion stays pending with the reason `AI_PROVIDER_NOT_CONFIGURED`.
- The teacher sees the suggestion with its quotes and can use it, change it or ignore it. A suggestion never changes a score or reaches a student by itself, and grades stay drafts until the teacher finalizes them.

## Consequences

- Kafka needs four new topics (`assessment.submitted.v2`, `grading.finalized.v2` and their `.dlq`); `kafka-init` creates them.
- An existing local setup needs `python scripts/upgrade_local.py` once. It adds `GRADING_MARKER_CLIENT_SECRET` to `.env` and creates the `grading-marker` client in the running Keycloak; Compose will not start without that secret, so with the stack down it adds the secret and asks to be run again once the stack is up. Then run `docker compose up -d --build` and `docker compose restart kong`, because Kong's CORS rules now allow `PUT`.
- Not built yet from the plan: a second AI opinion for low-confidence suggestions, a question bank, resits, and measuring how often teachers agree with suggestions. The classroom's marking scheme as extra context came in ADR 0005.
