# Engineering handover

This is the current state of the system. Why each rule exists is in `docs/decisions/` (ADRs 0001–0012), how to run and recover it is in `docs/development/operations.md`, and service boundaries and data ownership are in `docs/architecture/system.md`. Dated history lives in git and in the ADRs.

## Delivered architecture

The repository began empty. It contains exactly ten independent application services, their Dockerfiles and tests, an Angular client, and reproducible local Kong OSS / Keycloak / Kafka / PostgreSQL (pgvector) / S3-compatible object storage (SeaweedFS) infrastructure.

- **Go:** Identity/Classroom, Curriculum, Content, Assessment and Grading. Transactional, explicitly authorized operations with bounded request handling and SQL transactions.
- **Python/FastAPI:** deterministic Analytics and the four AI applications: ai-core (inference, ingest, rerank), the question chatbot (ADR 0002), question generation (ADR 0006) and performance interpretation (still a 501 placeholder).
- **Angular 21**, strict TypeScript, for the browser. Shell, SQL and configuration are operational artifacts; there is no third backend language and no extra business service.

## Repository layout

```text
apps/web/                          Angular features, API clients, shared components, E2E (Playwright)
services/{identity,curriculum,content,assessment,grading}/
                                   Independent Go modules, SQL migrations, tests, Dockerfiles
services/{analytics,ai-core,question-generation-agent,
          student-performance-agent,question-chatbot-agent}/
                                   Independent Python apps, requirements, tests, Dockerfiles
packages/platform/                 Go HTTP/auth/db/event/lock helpers (platformtest: test doubles)
packages/python-platform/          Python auth, errors, logging, Identity/token/ai-core clients, Kafka, SSE, DB pools and locks
contracts/{http,events}/           Ten OpenAPI documents and the event schemas (scripts/export_contracts.py writes them);
                                   the web's types (apps/web/src/api/generated) and Go contract_test.go follow them
infra/kong/                        Kong declarative config generator (bootstrap.py)
scripts/                           Setup, upgrade, checks, readiness, contracts, integration, stack tests, db tests, evals
docs/                              Original brief, ADRs, architecture, operations, this handover
.github/workflows/quality.yml      Service checks plus the live integration/E2E job
docker-compose.yml                 Complete local deployment (also run as the disposable `learning-test` project)
DESIGN.md / UX-CONTRACT.md         UI design and behavioural ownership
```

## Behaviour by area

### Identity and classrooms
- OIDC authorization code + PKCE. Services verify RS256, issuer, audience, expiry and organization; resource policy comes from Identity; identity headers confer nothing.
- An account is a teacher or a student, never both, and keeps the role it first registered with. A token with both roles is refused everywhere (403 `ROLE_CONFLICT`); `POST /v1/me` refuses a role change (409). Usernames and display names come from the token at each sign-in.
- Teachers create, list and delete classrooms and enroll students of their organization (`GET /v1/students`: search, `offset`, `limit` 1–50). `GET /v1/classrooms/{id}/students` is the roster by name, paged (`offset`, `limit` 1–500, default 50) with `total`; `GET /v1/classrooms/{id}/student-ids` is every enrolled student's id in the same order, which other services use to filter teacher lists. Unenrolling writes `identity.enrollment.removed.v1`; submissions and grades are kept and come back on re-enrolment (ADR 0010).
- Deleting a classroom sets `deleted_at`, removes its enrollments and writes `identity.classroom.deleted.v1`; every service purges its own data for it and remembers it (`purged_classrooms`) so late events are ignored. Not purged: ai-core's usage ledger and event copies still on Kafka until retention.
- Account deletion (ADR 0011): Profile tab, `DELETE /v1/me` soft-deletes, clears names, removes enrollments and writes `identity.user.deleted.v1`; a teacher who still owns a classroom gets 409 `OWNS_CLASSROOMS`. Assessment, Grading, Analytics and the chatbot purge the user's work and keep `deleted_users`. Identity removes the Keycloak user through the `identity-accounts` client (`signins.go`), retrying each user independently with backoff (migration 008), so users Keycloak keeps refusing don't hold back the rest. ai-core's ledger is kept.

### Curriculum (ADR 0007)
- One level, called topic: `GET/POST /v1/topics`, `GET/PATCH/DELETE /v1/topics/{id}` (delete archives), `POST /v1/topics/{id}/restore`, prerequisites (`GET/POST /v1/topics/{id}/prerequisites`, `DELETE .../{prerequisite}`) with serialized cycle detection that counts archived topics.
- `GET /v1/topics?archived=true` lists archived topics so old questions and evidence keep their names. The product never deletes a topic; only test cleanup does (`local_stack.delete_topics`).

### Content
- Authorized uploads of teaching material only (ADR 0009) into a restricted bucket, with SHA-256, original retrieval and UTF-8 text extraction. Older rubric and marking-scheme documents keep their type and stay teacher-only.
- PDFs up to 50 MiB are indexed asynchronously through ai-core (`index_jobs`, leased by two workers, 30-minute lease, backoff to `AI_UNAVAILABLE` after six attempts) into chunks with `vector(1536)` embeddings, figure crops and full-text indexes; `content.indexed.v1` goes out on success. `POST /v1/documents/{id}/reindex` for teachers.
- `POST /v1/search`: classroom access through Identity, vector and keyword legs, RRF, rerank through ai-core with RRF fallback, related figures and tables. Students only ever get teaching material.
- `DELETE /v1/documents/{id}` (teacher) atomically removes the row, chunks and indexing job, writes `content.deleted.v1` and queues `object_cleanup_jobs`. A leased worker removes the object prefix after commit and retains failures for retry; classroom purges use the same queue (ADR 0012). An hourly sweep also removes bucket objects older than 6 hours that no row names, re-checking each under the document row's lock just before removal.

### Assessment (ADRs 0004, 0005, 0008)
- Up to 50 questions: multiple choice, numerical, short answer (accepted answers) and free response (rubric, optional model answer), points per question, optional marking points (`marking_scheme`, up to 5 passages; teacher-only), and optional `sources`/`origin: "generated"` from question generation.
- Drafts are edited with `PUT`, published with `POST /publish`. A published assessment stays fully editable until a student submits; then only title, due date and late policy change (`PATCH`, fields left out are kept). A replacement or a due-date change on a published assessment writes `assessment.due_changed.v1` and recomputes `late`.
- `DELETE` answers 409 `HAS_SUBMISSIONS` when anyone (enrolled or not) has submitted; `?with_submissions=true` deletes it with its submissions and writes `assessment.deleted.v1`. The teacher list returns `submissions` (enrolled students only) and `has_submissions` (anyone).
- Students: opening and due dates, late policy (accept and flag, or 409 `CLOSED`, re-checked under the lock), autosaved answers, one submission each, an answer-key-free projection. Answers or saved work naming questions the teacher has since replaced get 409 `ASSESSMENT_CHANGED`. `GET /v1/assessments/{id}/submission` gives the submitting student their own answers, `submitted_at` and `late` (404 before they submit, 403 for teachers).
- Submissions go out as `assessment.submitted.v4`.

### Grading (ADR 0004)
- Kafka-triggered deterministic draft grading with exact decimal arithmetic and inclusive tolerance. Unmatched short answers and free responses wait for the teacher (`needs_marking`), optionally with an AI suggestion from `marking_jobs` workers through ai-core as `grading-marker`, billed to the assessment's creator, checked against the rubric before storing.
- Teacher review is mandatory; finalization writes the record and an audit entry atomically. `grading.finalized.v3` goes out when the grade is released (at finalization, or at `POST /v1/evaluations/release` for manual release), so Analytics only ever holds released grades. Students see only their own released results (`GET /v1/evaluations/mine`).
- Teacher lists and counts include only students on Identity's roster (`platform.ClassroomRoster`, complete however large the class). A marking job for a removed student ends `STUDENT_REMOVED` without a model call. `GET /v1/evaluations` is paged (`offset`, `limit` 1–100, default 20) with `total`; `GET /v1/evaluations/{id}` reads one grade for the teacher; `/counts` takes an optional `assessment_id`; a release releases only currently enrolled students' grades (ADR 0010).

### Analytics
- Consumes finalized evidence into per-topic accuracy, attempted/correct counts, part marks and an explicit insufficient-evidence status; no mastery-probability claim. `weakness_indicator` ("Needs practice") is five or more questions under 60% correct.
- `GET /v1/performance?classroom_id=` pages the current roster (from Identity with the teacher's token). The consumer group comes from `KAFKA_GROUP` (operations.md, "Rebuilding analytics").

### AI applications
- **ai-core** (private, `ai-client` role): `/v1/ingest` (local PyMuPDF text/layout extraction, selective vision for uncertain graphics/tables/OCR, configurable chunks; 500 pages / 5000 chunks max, `TOO_LARGE`), `/v1/inference`, `/v1/rerank`. Versioned operational caches reuse prepared PDFs and exact embedding inputs. Every call names the billed user; PostgreSQL reservations enforce the allowance before each provider attempt and share `AI_USER_CONCURRENCY` (10) across replicas (ADR 0012). Actual usage settles reservations; uncertain or failed accounting conservatively retains them. Tokenizer data is committed for both ai-core and the chatbot, with no runtime download.
- **Question chatbot** (`/api/chatbot`, own `chatbot` database): skips rewriting simple standalone definitions; retains rewriting for history, ambiguity and multiple topics. Searches the primary query first and adds searches for uncovered topics, retaining the calibrated rerank floor. Evidence fits `CONTEXT_TOKENS` (8000 default) or the turn refuses without an answer call. Content receives the user's own token. Answers stream with citations and are always saved (`interrupted` on disconnect). Citations of deleted materials are blanked (`removed: true`).
- **Question generation** (`/api/question-generation`, stateless): `POST /v1/questions` and `POST /v1/marking-guidance` for teachers; drafts must cite given passages and quote evidence found in them. `RELEVANCE_FLOOR` is 0.5, calibrated on the eval corpus (operations.md).
- The two agents share their ai-core and Content transport in `learning_transport`.
- **Student performance:** `/v1/performance_interpretation` validates and returns 501 `AI_CAPABILITY_NOT_IMPLEMENTED`.

### Web
- Teachers land on the Class dashboard, students on Home. Sections: Classrooms, Curriculum (teacher), Materials with indexing status and delete, Ask the materials (streamed chat with sources), Assessments (editor with generation and marking guidance; student taking with autosave), Grading review with inline marking and AI suggestions, My results, Learning performance / My progress, Profile.
- Every list loads through `shared/section.ts` and shows loading, empty and failure states; names come from the shared `CurriculumNames` service; dialogs use `app-confirm-dialog`. The app reads its gateway and Keycloak URLs from nginx's `/config.json`.
- Classroom/topic/document/assessment APIs expose additive `/page` endpoints (`offset`, `limit` 1–100, default 20, `{items,total}`); legacy array endpoints retain their old caps. Materials and assessments use `PagedSection`; picker metadata loads all bounded pages. Progress uses `/progress/page`; assessment detail reads coalesce into authorized batches of up to 50. Class dashboard roster reads only its visible page plus the total.

## Events and persistence

- **Current topics:** `content.uploaded.v1`, `content.indexed.v1` (no consumer), `content.deleted.v1`, `assessment.submitted.v4`, `assessment.deleted.v1`, `assessment.due_changed.v1`, `grading.finalized.v3`, `identity.classroom.deleted.v1`, `identity.enrollment.removed.v1`, `identity.user.deleted.v1`. Older versions (`assessment.submitted.v1–v3`, `grading.finalized.v1–v2`) were retired on 2026-10-19: nothing produces or reads them, and their schemas are gone (ADR 0007 amendment). Consumed topics have `.dlq` topics; `kafka-init` creates them all and sets 7-day retention on submission, finalized-grade and dead-letter topics.
- Producers write outboxes in the business transaction and publish at least once; only the oldest unpublished event per topic and key is due, so keys stay ordered. Outboxes prune events published over 7 days ago. Consumers commit offsets only after the inbox/business transaction or a confirmed dead-letter write; event-ID plus business-key deduplication covers replays.
- Eight service-owned databases (question generation and performance interpretation are stateless) with independent non-superuser credentials. All advisory locks use `platform.Lock`/`learning_transport.db.lock` (`hashtextextended(scope || ':' || key, 0)`).

## Verification

- **Local gates:** `python scripts/check.py` runs formatting, lint, types, unit tests and builds for every Go module, Python service and the web app, plus `export_contracts.py --check`, the web's `npm run contracts:check` (its generated API types) and each Go service's `contract_test.go` (its JSON structs against the contract's named schemas). Database-backed tests skip there; `scripts/db_tests.py` runs them against PostgreSQL.
- **Stack tests:** `python scripts/stack_tests.py [integration] [db] [e2e]` builds a disposable `learning-test` Compose project (Kong 18000, Keycloak 18081, web 14200), runs `integration.py`, `db_tests.py` and the Playwright suite, then removes it, beside a running development stack. Both test scripts refuse the development stack, and the test project name with any non-test port, unless given `--dev` / `E2E_DEV_STACK=1`. The test stack has its own realm import (`.local/test-stack/`), so only it allows the test web origin. Run in a Linux cloud container on 2026-10-18 and on Windows by 2026-10-19.
- `integration.py` exercises the real gateway, Keycloak, PostgreSQL, S3 and Kafka: authorization failures, schema validation of responses and events, consumer restart, duplicate-event idempotency, classroom/assessment/account purges, late events after purges, rate limiting per session and database isolation.
- GitHub Actions runs the full gate on `main`, including the live integration and Playwright jobs.
- **Real models (desktop, 2026-10-19):** ingest of the 22-PDF eval corpus, the chat eval (99 turns: 0 leaks, 0 obeyed injections, 1 failure, `waterfall-weakness`, refused as on 2026-10-13), question generation, marking guidance and AI marking suggestions ran against OpenAI and Voyage, for $0.08 in all on ai-core's ledger.
- **Not verified:** the premium UI audit (its tool isn't in the repository), Neon, and any production deployment.

## Operational notes

- Kong re-resolves service names every 5 seconds; a new route still needs `docker compose restart kong`.
- Kong rate-limits per `Authorization` header (120 a minute per signed-in session).
- Lock ids changed with the shared lock scheme (2026-10-17): stop every replica of a service before starting a new version.
- Upgrading an existing `.env`: `python scripts/upgrade_local.py` (adds missing values, the chatbot database and Keycloak clients, idempotent), then `docker compose up -d --build`.

## Incomplete requirements and risks

The full requested definition of done is **not claimed**. The core workflow is implemented, but these remain incomplete or narrower than the brief:

- Question bank, resits, numerical generated questions and student self-practice. Each student can submit an assessment once.
- Document revisions beyond revision 1, lesson-plan persistence, and the performance interpretation agent.
- Assessment trends, richer teacher summaries, and archival/reconciliation beyond Kafka's 7-day retention.
- Offset pagination can drift under concurrent insertions; metadata pickers still accumulate their full authorized list. Cursor pagination and server-side picker search remain future work.
- Production Kafka TLS/SASL/ACLs, network policies, per-service machine identities, managed OIDC persistence, signing-key rollover, separate migration/runtime roles, backups/restore testing, secret management and dependency/image audits.
- S3/SQL atomicity relies on retaining ambiguous originals plus the hourly sweep, not a staged-upload saga.
- Reservation estimates depend on configured model prices and image/token allowances. Uncertain provider outcomes or ledger-write failures retain conservative holds; operators must investigate these instead of blindly releasing them.
- Static checks and focused browser tests are not a comprehensive accessibility, multi-browser, load, chaos or security assessment.

Recommended next: question bank and resits (ADR 0004's "not built yet"), cursor pagination, then the performance interpretation agent, each behind its own ADR as AGENTS.md requires. Preserve teacher approval and evidence provenance throughout.
