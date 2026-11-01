# Engineering handover

This is the current state of the system. Why each rule exists is in `docs/decisions/` (ADRs 0001–0017), how to run, upgrade and recover it is in `docs/development/operations.md`, and service boundaries and data ownership are in `docs/architecture/system.md`. Dated history lives in git and in the ADRs: when something changes, update the section it belongs to here, and keep only the latest run of each kind under Verification.

## Delivered architecture

The repository began empty. It contains exactly ten independent application services, their Dockerfiles and tests, an Angular client, and reproducible local Caddy edge / Kong OSS / Keycloak / Kafka / PostgreSQL (pgvector) / S3-compatible object storage (SeaweedFS) infrastructure.

- **Go:** Identity/Classroom, Curriculum, Content, Assessment and Grading. Transactional, explicitly authorized operations with bounded request handling and SQL transactions.
- **Python/FastAPI:** deterministic Analytics and the four AI applications: ai-core (inference, ingest, rerank), the question chatbot (ADR 0002), question generation (ADRs 0006, 0015) and performance interpretation (ADR 0014).
- **Angular 21**, strict TypeScript, for the browser. Shell, SQL and configuration are operational artifacts; there is no third backend language and no extra business service.

## Deployment layout

- The `edge` container (Caddy, `infra/edge/Caddyfile`) is the public entry. It ends TLS for `app.localhost` (the web app, and Kong under `/api` on the same origin) and `auth.localhost` (Keycloak). Kong (8000) and Keycloak (8081) are published on 127.0.0.1 for scripts and tests only; the web app is not published.
- Three networks: `edge` (edge, web, Kong, Keycloak), `app` (applications, Keycloak, Kong) and `data` (PostgreSQL, Kafka, SeaweedFS; `internal`). Only services that own data join `data`; question generation and performance interpretation cannot reach a database or the broker.
- Every container has CPU and memory limits; long-running ones restart without a retry cap.
- Keycloak runs in production mode on its own `keycloak` database. The one-shot `keycloak-migrate` moved older H2 realms into PostgreSQL keeping every user's id; the realm requires HTTPS except from private addresses. Upgrade and rollback: operations.md, "Edge, networks and Keycloak on PostgreSQL".

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
infra/edge/                        Caddy edge config
infra/keycloak/                    Keycloak H2-to-PostgreSQL migration
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
- Native formats (ADR 0013, migration 006): Markdown (5 MiB), PPTX and DOCX (50 MiB) are read locally into heading-aware chunks through the same queue, caches and spend checks. Headings, native tables and PPTX speaker notes are kept; citations show slides or section paths, not page numbers. Office images, charts and SmartArt are not interpreted. Plain `.txt` is extraction-only. Existing documents are not reindexed automatically.
- `POST /v1/search`: classroom access through Identity, vector and keyword legs, RRF, rerank through ai-core with RRF fallback, related figures and tables. Students only ever get teaching material.
- `DELETE /v1/documents/{id}` (teacher) atomically removes the row, chunks and indexing job, writes `content.deleted.v1` and queues `object_cleanup_jobs`. A leased worker removes the object prefix after commit and retains failures for retry; classroom purges use the same queue (ADR 0012). An hourly sweep also removes bucket objects older than 6 hours that no row names, re-checking each under the document row's lock just before removal.
- `services/ai-core/pdf_structure_probe.py` is a standalone offline CLI for PDF structure research, not connected to the app (`docs/development/structure-aware-pdf-research.md`).

### Assessment (ADRs 0004, 0005, 0008, 0017)
- Up to 50 questions: multiple choice, numerical, short answer (accepted answers) and free response (rubric, optional model answer), points per question, optional marking points (`marking_scheme`, up to 5 passages; teacher-only), and optional `sources`/`origin: "generated"` from question generation.
- Drafts are edited with `PUT`, published with `POST /publish`. A published assessment stays fully editable until a student submits; then only title, due date and late policy change (`PATCH`, fields left out are kept). A replacement or a due-date change on a published assessment writes `assessment.due_changed.v1` and recomputes `late`.
- `DELETE` answers 409 `HAS_SUBMISSIONS` when anyone (enrolled or not) has submitted; `?with_submissions=true` deletes it with its submissions and writes `assessment.deleted.v1`. The teacher list returns `submissions` (enrolled students only) and `has_submissions` (anyone).
- Students: opening and due dates (a submission is re-checked under the lock against the opening as stored, 404 before it), late policy (accept and flag, or 409 `CLOSED`, re-checked under the lock), autosaved answers, one submission each, an answer-key-free projection. Answers or saved work naming questions the teacher has since replaced get 409 `ASSESSMENT_CHANGED`. `GET /v1/assessments/{id}/submission` gives the submitting student their own answers, `submitted_at` and `late` (404 before they submit, 403 for teachers).
- Submissions go out as `assessment.submitted.v4`.
- Create and edit requests take only the contract's editable fields: a top-level `id`, `published_at`, `assigned`, `resit_of` or `assignees` is 422 (before ADR 0017 `id` and `published_at` were silently accepted). Only the server sets whether an assessment is assigned, its `resit_of` and its assignees.
- **Question bank** (ADR 0017, migration 006): one per teacher, up to 2,000 questions (409 `BANK_FULL`, counted under an exclusive per-teacher bank lock). `GET/POST /v1/bank/questions` (paged, newest change first; filters `topic_id`, `type`, `q`, `origin`; with `classroom_id` + `student_id`, `answered` per question and `exclude_answered`), `GET/PUT/DELETE /v1/bank/questions/{id}`, owner only (404 otherwise). A bank question is an assessment question without its id, `sources` or `marking_scheme`; a bank request carrying passages is 422. `POST` with `{assessment_id, question_id}` saves an assessment question, passages stripped, also from published and submitted assessments; 409 `ALREADY_IN_BANK` (with the bank id) or `ASSESSMENT_CHANGED`. Each assessment question can name its bank question in `question_bank_links`, a plain reference outside the question; a link to a deleted bank question is ignored. "Answered" spans all of the teacher's classrooms, including ones the student has left.
- **Resits**: `POST /v1/assessments/{id}/resits` makes a draft in the same classroom from a published assessment the teacher owns: 1–100 assigned students on the roster, 1–50 distinct questions copied from the original (with their passages) and then the bank. It copies the late and release policies, AI marking only if a teacher-marked question remains, and no dates. `PUT /v1/assessments/{id}/assignees` replaces a resit's list (not empty; a student who submitted can't be removed, 409 `HAS_SUBMISSIONS`; a removed student's saved answers are deleted; 409 `NOT_ASSIGNED` on an ordinary assessment). An assigned assessment is visible only to listed students on the roster: every student path answers an unlisted student as for an unopened assessment (404, left out of lists and counts). Students see `resit: true`; teacher summaries and progress gain `resit_of` and `assignee_count`. The original grade is never changed.
- Purges: a deleted teacher's bank, a deleted student's assignee rows; a classroom purge cascades to links and assignees and leaves the bank. Resit creation takes up to 101 shared advisory locks (100 students and the classroom); bank writes, assignee changes and resit creation make no HTTP call while holding a lock.

### Grading (ADRs 0004, 0016)
- Kafka-triggered deterministic draft grading with exact decimal arithmetic and inclusive tolerance. Unmatched short answers and free responses wait for the teacher (`needs_marking`), optionally with an AI suggestion from `marking_jobs` workers through ai-core as `grading-marker`, billed to the assessment's creator, checked against the rubric before storing.
- Teacher review is mandatory; finalization writes the record and an audit entry atomically. `POST /v1/evaluations/finalize` `{assessment_id, reason?}` finalizes every enrolled student's fully marked draft in one transaction and returns `finalized` and `needs_marking`; `/counts` adds `ready`. `POST /v1/evaluations/{id}/reopen` (required 1–1000 character reason) makes a finalized grade a draft at its next `revision` with an audit entry; the student no longer sees it until it is finalized again, and a released one writes `grading.reopened.v1`. The review page has "Reopen to correct" and "Finalize all fully marked", both behind confirmation dialogs.
- `grading.finalized.v3` (optional `revision`, absent = 1) goes out when the grade is released (at finalization, or at `POST /v1/evaluations/release` for manual release), so Analytics only ever holds released grades. Students see only their own released results (`GET /v1/evaluations/mine`, the newest 100; `/mine/page` pages them with `total`).
- Marking or finalizing a grade by id also needs its student on the roster (409 `STUDENT_REMOVED` otherwise); reading it does not.
- Teacher lists and counts include only students on Identity's roster (`platform.ClassroomRoster`, complete however large the class). A marking job for a removed student ends `STUDENT_REMOVED` without a model call. `GET /v1/evaluations` is paged (`offset`, `limit` 1–100, default 20) with `total`; `GET /v1/evaluations/{id}` reads one grade for the teacher; `/counts` takes an optional `assessment_id`; a release releases only currently enrolled students' grades (ADR 0010).

### Analytics
- Keeps the newest released revision of each grade's evidence, with a `withdrawn` flag for a reopened grade until it is finalized again (migration 007), so an older, repeated or out-of-order event changes nothing. Deploy Analytics before or with Grading when event versions change.
- Turns evidence into per-topic accuracy, attempted/correct counts, part marks and an explicit insufficient-evidence status; no mastery-probability claim. `weakness_indicator` ("Needs practice") is under 60% of the marks (part marks included) on the topic's latest ten questions, with at least five; `recent_attempted` and `recent_marks_accuracy` report those latest questions.
- `GET /v1/performance?classroom_id=` pages the current roster (from Identity with the teacher's token). The consumer group comes from `KAFKA_GROUP` (operations.md, "Rebuilding analytics").

### AI applications
- **ai-core** (private, `ai-client` role): `/v1/ingest` (local PyMuPDF text/layout extraction, selective vision for uncertain graphics/tables/OCR, configurable chunks; 500 pages / 5000 chunks max, `TOO_LARGE`), `/v1/inference`, `/v1/rerank`. Versioned operational caches reuse prepared PDFs and exact embedding inputs. Every call names the billed user; PostgreSQL reservations enforce the allowance before each provider attempt and share `AI_USER_CONCURRENCY` (10) across replicas (ADR 0012). Actual usage settles reservations; uncertain or failed accounting conservatively retains them. Non-streamed `generate` takes an optional `timeout` that caps the generation budget, so a reply the caller stopped waiting for is not finished and billed; a structured reply that runs out of time is 504 `AI_TIMEOUT` (still charged). Tokenizer data is committed for both ai-core and the chatbot, with no runtime download.
- **Question chatbot** (`/api/chatbot`, own `chatbot` database): skips rewriting simple standalone definitions; retains rewriting for history, ambiguity and multiple topics (`query_rewrite.v3`). Searches the primary query first and adds searches for uncovered topics; passages under `TOPIC_FLOOR` (0.5) never reach the model, and the global relevance floor is 0.61. Evidence fits `CONTEXT_TOKENS` (8000 default) or the turn refuses without an answer call. After streaming, a local check adds a notice when a requested topic has no cited passage. Content receives the user's own token. Answers stream with citations and are always saved (`interrupted` on disconnect); a malformed stream event is `AI_PROVIDER_FAILED`. Citations of deleted materials are blanked (`removed: true`).
- **Question generation** (`/api/question-generation`, stateless): `POST /v1/questions` and `POST /v1/marking-guidance` for teachers; drafts must cite given passages and quote evidence found in them, and passages under `RELEVANCE_FLOOR` (0.5) are dropped. A reviewer pass (ADR 0015) checks each draft against only its cited passages and sends failures back with the reason, up to two retries within `QUESTION_REVIEW_DEADLINE`; counts are per question type, and a teacher can skip the review per request.
- **Performance interpretation** (`/api/performance-agent`, stateless): `POST /v1/performance_interpretation` for a classroom's teacher and one enrolled student. It pages Grading (up to five pages of 100) until it has the newest 20 finalized results. A bounded tool loop (`PERFORMANCE_MAX_TURNS` 5, `PERFORMANCE_DEADLINE` 120 s) over Analytics, Grading, Assessment and Curriculum; each turn lists the question references a report may cite, the topics it may recommend and the `suggested_checks` (prerequisite and class checks) not yet made. Findings cite only questions it was shown, misconceptions only answers it read that lost marks (ADR 0014). A tool turn that is unreadable or fails goes straight to the report, counted in `dropped`.
- The agents share their ai-core and Content transport in `learning_transport`, which passes ai-core a `timeout` `REPLY_MARGIN` (5 s) under the agent's own deadline. Planned agentic work: `docs/architecture/agentic-workflows.md`.

### Web
- Teachers land on the Class dashboard, students on Home. Sections: Classrooms, Students, Curriculum (teacher), Materials with indexing status and delete, Ask the materials (streamed chat with sources), Assessments (list, editor and preview with a generation side panel; student full-page taking with autosave), Question bank (teacher), Grading review (queue with grade panel and next-to-mark navigation, inline marking, AI suggestions), My results, Learning performance / My progress, Profile.
- Every list loads through `shared/section.ts` and shows loading, empty and failure states; names come from the shared `CurriculumNames` service; dialogs use `app-confirm-dialog`. The app reads its gateway and Keycloak URLs from nginx's `/config.json`.
- Classroom/topic/document/assessment APIs expose additive `/page` endpoints (`offset`, `limit` 1–100, default 20, `{items,total}`); legacy array endpoints retain their old caps. Materials and assessments use `PagedSection`; picker metadata loads all bounded pages. Progress uses `/progress/page`; assessment detail reads coalesce into authorized batches of up to 50. Class dashboard roster reads only its visible page plus the total.
- Question bank and resits (ADR 0017): `/question-bank` lists, filters, writes, edits and deletes the teacher's bank questions. The editor's side panel has "Add from bank" beside generation; each question has "Save to bank" (or "Update bank copy" and "Save as new" once linked), disabled with a note while the form has unsaved changes, and the form round-trips each question's `bank_question_id`. "Create resit" on a published assessment in the list and in Grading review opens the builder at `/assessments?resit=<id>`: it suggests students who lost marks on finalized grades (up to five pages of 100, saying so when more remain) and bank questions on those topics with answered ones hidden, copies chosen questions from the original, and opens the new draft in the editor. Resits carry a "Resit" label for teachers and students, and the class dashboard counts submissions out of `assignee_count`. There is no screen yet for changing a resit's students after it is created (the API and client exist), and no entry from Learning performance.

## Events and persistence

- **Current topics:** `content.uploaded.v1`, `content.indexed.v1` (no consumer), `content.deleted.v1`, `assessment.submitted.v4`, `assessment.deleted.v1`, `assessment.due_changed.v1`, `grading.finalized.v3`, `grading.reopened.v1`, `identity.classroom.deleted.v1`, `identity.enrollment.removed.v1`, `identity.user.deleted.v1`. Older versions were retired on 2026-10-19 (ADR 0007 amendment). Consumed topics have `.dlq` topics; `kafka-init` creates them all and sets 7-day retention on submission, finalized-grade and dead-letter topics.
- Producers write outboxes in the business transaction and publish at least once; only the oldest unpublished event per topic and key is due, so keys stay ordered. Outboxes prune events published over 7 days ago. Consumers commit offsets only after the inbox/business transaction or a confirmed dead-letter write; event-ID plus business-key deduplication covers replays.
- Nine service-owned databases (eight application databases plus Keycloak's; question generation and performance interpretation are stateless) with independent non-superuser credentials. All advisory locks use `platform.Lock`/`learning_transport.db.lock` (`hashtextextended(scope || ':' || key, 0)`).

## Verification

- **Local gate:** `python scripts/check.py` runs formatting, lint, types, unit tests and builds for every Go module, Python service and the web app, plus `export_contracts.py --check`, the web's `npm run contracts:check` and each Go service's `contract_test.go`. Database-backed tests skip there; `scripts/db_tests.py` runs them against PostgreSQL.
- **Stack tests:** `python scripts/stack_tests.py [integration] [db] [e2e]` builds a disposable `learning-test` Compose project (edge 18443, Kong 18000, Keycloak 18081) beside the development stack, runs `integration.py`, `db_tests.py` and the Playwright suite, then removes it. Both test scripts refuse the development stack unless given `--dev` / `E2E_DEV_STACK=1`. `integration.py` exercises the real gateway, Keycloak, PostgreSQL, S3 and Kafka: authorization failures, schema validation of responses and events, consumer restart, duplicate-event idempotency, purges, late events after purges, rate limiting and database isolation. GitHub Actions runs the full gate on `main`, including the live integration and Playwright jobs.

Latest runs:

| Check | Date | Result |
|---|---|---|
| `scripts/check.py --keep-going` | 2026-11-01 | All 15 lanes pass (111 s), with the question bank and resits. The chatbot lane can fail under the full parallel run on Windows with `WinError 10055` (socket buffers); it passes on its own. |
| Stack tests (integration, db, e2e) with `--live-ai` | 2026-11-01 | All pass on Windows behind the edge, with the question bank and resits: integration 201 s with every AI path required to work (indexing, search, chat, generation, marking suggestions, interpretation), every database suite 69 s (Assessment's bank, assignee and resit races included), 9/9 Playwright 98 s (`resits.spec.ts` included). Playwright's generation and chat specs stub model responses, so only integration checks real model answers; a fresh volume does not exercise the H2-to-PostgreSQL migration or migration 006 on existing data. |
| Agent evals (`scripts/eval/`) | 2026-10-27 | Reviewer caught 11/13 planted faults with no good draft failed; interpretations accepted 10/10 with expected tool use 1.0. $0.023 in all. Details: operations.md, "Agent evals". |
| Retrieval and chat evals | 2026-10-20 | Retrieval hit@1 0.924, MRR 0.950, no leaks; chat 3 failed cases (`waterfall-weakness`, `multi-review-testing`, `multi-persistence-design`). Follow-up changes await a paid rerun: `rag-quality-followup.md`. The 0.5 floors have not been re-run against the corpus. |
| Premium UI audit | 2026-10-21 | 64 findings the tool raises on Angular `(click)` and shared textarea styles; reviewed, not a clean pass. |

## Operational notes

- Kong re-resolves service names every 5 seconds; a new route still needs `docker compose restart kong`.
- Kong rate-limits per `Authorization` header (120 a minute per signed-in session).
- Stop every replica of a service before starting a version that changes lock ids.
- Upgrading an existing `.env` or stack: `python scripts/upgrade_local.py` (adds missing values, databases and Keycloak clients, idempotent), then `docker compose up -d --build`. Trust the edge's local certificate authority once (README).
- The owner's $5 aggregate cap covers all paid work; preserve development and test usage ledgers and check remaining allowance before paid runs.

## Incomplete requirements and risks

The full requested definition of done is **not claimed**. The core workflow is implemented, but these remain incomplete or narrower than the brief:

- Numerical generated questions and student self-practice (its own ADR). Each student can submit an assessment once; a second attempt is a resit (ADR 0017). Filling bank gaps with question generation and the adaptive resit agent (agentic workflow 4) are not built, nor sharing banks between teachers or "best of" grades.
- Document revisions beyond revision 1 and lesson-plan persistence. Performance interpretations are drafts on request: none run on finalization or are saved.
- Assessment trends, richer teacher summaries, and archival/reconciliation beyond Kafka's 7-day retention.
- Offset pagination can drift under concurrent insertions; metadata pickers still accumulate their full authorized list. Cursor pagination and server-side picker search remain future work.
- PDF figure/table kind failures (Basic Security p16, Automation and Deployment p9) need extraction work and reindexing.
- Production: real certificates instead of the edge's local CA, Kafka TLS/SASL/ACLs, per-service machine identities, Keycloak database backups, signing-key rollover, separate migration/runtime roles, backups/restore testing, secret management and dependency/image audits.
- Assessment does not consume `content.deleted.v1`, so source passages and marking points of a deleted material stay in assessment questions, and in resits copied from them. They are teacher-only; the bank holds no passages, so it does not widen this.
- A resit holds up to 101 shared advisory locks (100 students and the classroom). PostgreSQL's lock table holds `max_locks_per_transaction` (64) × `max_connections`, so raising the 100-student cap or running many large resits at once needs that budget checked (operations.md).
- S3/SQL atomicity relies on retaining ambiguous originals plus the hourly sweep, not a staged-upload saga.
- Reservation estimates depend on configured model prices and image/token allowances. Uncertain provider outcomes or ledger-write failures retain conservative holds; operators must investigate these instead of blindly releasing them.
- Static checks and focused browser tests are not a comprehensive accessibility, multi-browser, load, chaos or security assessment.

Recommended next: a screen for changing a resit's students, student self-practice, filling bank gaps with question generation and the adaptive resit agent, interpretation on finalization, cursor pagination, and the next agentic workflows in `docs/architecture/agentic-workflows.md`, each behind its own ADR as AGENTS.md requires. Preserve teacher approval and evidence provenance throughout.
