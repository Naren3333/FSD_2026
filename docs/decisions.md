# Design decisions

This file holds the project's architecture decision records. Each record states a problem, the rule chosen for it, the reasons and evidence behind the rule, and what it costs. The numbers match the `adr NNNN` references in code comments, so a comment such as `adr 0020` points to the section with that number. A later record can amend or supersede an earlier one. Each section gives the current rule, notes briefly when it was amended, and names the record that changed it; where two records disagree, the later one holds.

## Index

| No. | Title | Decision |
|---|---|---|
| 0001 | Independent services and explicit ownership | Ten services, one database per persistent service, Kong and Keycloak, Kafka outboxes. |
| 0002 | Integrating the RAG and chat services | Content owns chunks and vectors, ai-core does model work, the chatbot searches with the user's own token. |
| 0003 | Per-user daily AI spend caps | ai-core keeps a ledger and refuses paid calls over $5 (student) or $10 (teacher) a day. |
| 0004 | Assessment lifecycle, teacher marking and AI marking | Drafts, dates, written questions with rubrics; AI marking is a suggestion inside Grading. |
| 0005 | Marking-scheme passages as context | Passages the teacher picks travel with the question to the AI marker. |
| 0006 | Question generation and drafted marking guidance | A stateless pipeline drafts grounded questions and guidance for the teacher to review. |
| 0007 | The curriculum is a flat list of topics | One level, the topic; "skill" is renamed "topic" everywhere. |
| 0008 | Deleting assessments students have submitted | A confirmed delete purges submissions and grades in every owner through an event. |
| 0009 | Retire uploaded marking schemes | Content takes teaching material only; teachers can delete a material. |
| 0010 | No stale data after a removal or deletion | Enrolment changes, document deletes and due-date changes are events; 7-day retention. |
| 0011 | Users can delete their own account | Identity deletes the account; each service purges its own data. |
| 0012 | Efficient retrieval and durable cleanup | Durable object cleanup, text-first PDFs, caches, spend reservations, pagination. |
| 0013 | Native structured documents | Markdown, PPTX and DOCX are indexed with their headings, without Office or models. |
| 0014 | Performance interpretation agent | A bounded tool-using loop drafts a checked interpretation of one student. |
| 0015 | Question generation critic loop | A reviewer pass checks each draft; failures are rewritten, within six calls. |
| 0016 | Reopening finalized grades | A finalized grade can be reopened with a reason; fully marked drafts finalize together. |
| 0017 | Question bank and resits | Each teacher has a bank of copied questions; a resit is an assessment for chosen students. |
| 0018 | Platform monitoring and operator access | A stateless collector reports readiness, latency, dead letters and AI spend. |
| 0019 | School admins | A per-school admin creates accounts and classrooms and enrols students. |
| 0020 | Marking critic | A blind second marking flags doubtful AI suggestions for the teacher. |
| 0021 | Class-insights copilot | A second route on the performance agent's loop answers class questions with checked figures. |
| 0022 | Student self-practice | Students generate reviewed, ungraded practice from class materials, billed to themselves. |
| 0023 | Model profiles and reasoning | ai-core offers `default`, `check` and `fast` profiles, reasoning headroom, retries and one re-ask. |
| 0024 | Marking accuracy and critic profile | Per-question schemas, folded quote matching, exact-match short answers, a marking eval. |
| 0025 | Chat answer support check | One check call per cited answer, after `done`, notes sentences the passages do not support. |
| 0026 | Independent question review | The reviewer runs on the `check` profile with key checks; marking guidance is reviewed. |

## 0001 Independent services and explicit ownership

**Status:** accepted for the foundation. The record gives no date or approver.

**Context.** The project needs ten deployable applications with clear ownership of data, identity and events.

**Decision.**
- Exactly ten application services. Go owns the transactional classroom, curriculum, content, assessment and grading operations. Python/FastAPI owns deterministic analytics and four AI applications. Angular 21 LTS with strict TypeScript owns the browser.
- Kong OSS does public routing, JWT signature and expiry checks, rate limiting, request limits, CORS and correlation ids. Keycloak provides OIDC; no custom passwords or signing protocols. Every service still checks issuer, audience, expiry, role and resource access itself.
- Each persistent service has its own PostgreSQL database and non-superuser login (six at first; later records add more). No cross-database queries or shared domain models. Neon is the production target through per-service `DATABASE_URL` values with verified TLS. Each local owner can migrate only its own database.
- Kafka carries committed facts through transactional outboxes, with at-least-once delivery. Consumer inboxes and business uniqueness constraints stop duplicate effects. Outbox retries are persistent and capped; failed rows are kept for operator recovery. Poison records go to a dead-letter topic before acknowledgement; transient exhaustion stops processing without acknowledging.
- S3-compatible object storage keeps original content (MinIO at first, SeaweedFS since 0002). Content owns metadata, extracted text and, since 0002, pgvector embeddings, versioned by model, dimensions, chunker and source revision. A new embedding version is backfilled into a separate index, checked for coverage, then switched atomically.
- AI was deferred: four separately authenticated Python applications validated their contracts and returned explicit 501 errors, and never fetched private content or invented output. Later records (0002, 0006, 0014, 0020–0022) authorize real behaviour one capability at a time.

**Why.** Go's standard HTTP stack and static typing suit bounded transactional workflows. Python gives typed contracts without model dependencies. Keycloak avoids writing password or token handling. Per-service databases and outboxes keep each service the only writer of its own facts, and durable grading events can rebuild analytics within broker retention. Version choices are backed by the Angular compatibility table (https://angular.dev/reference/versions), Kafka's Docker guide (https://kafka.apache.org/41/getting-started/docker/) and Kong's OSS JWT plugin page (https://developer.konghq.com/plugins/jwt/).

**Consequences and limits.**
- The OSS JWT plugin uses a provisioned public key, so key rotation means regenerating the gateway configuration. Enterprise OIDC plugins are not assumed.
- Production should split migration and runtime roles. Long-term event archival is an operational prerequisite.
- Patch versions are pinned in lockfiles and images; updating them means rerunning the checks.
- Later records changed parts of this one: ai-core and the chatbot gained databases (0002, 0003), and school admins took over classroom management (0019).

## 0002 Integrating the RAG and chat services

**Status:** accepted 2026-10-13 by the project owner. Supersedes the "AI is deferred" part of 0001 for the capabilities named here.

**Context.** `fsd_test` was a separate, working harness for retrieval-augmented generation and chat over teachers' PDFs. Its `knowledge` service did PDF ingest (vision, table, figure and OCR passes), hybrid pgvector and full-text search and Voyage reranking. Its `assistant` service did threads, query rewrite and cited streaming answers. It trusted a client-supplied `X-Principal` header and used two Neon projects. The owner decided to fold it into this project rather than add services, keep the ten-service layout, and use local Docker Postgres for development.

**Decision.**
- Content stays the only owner of documents, and now also of their chunks, embeddings and search index, in its own database with pgvector. Search is an authorized Content API scoped by classroom, with the same Identity membership check as every other Content read. No other service reads its tables.
- ai-core does the model work: PDF ingest into chunks with embeddings, query embeddings, reranking and generation. It accepts only callers with the `ai-client` role. Provider keys (OpenAI, Voyage) exist only in its environment.
- question-chatbot-agent does grounded answering. It owns a `chatbot` database for threads and messages, is published through Kong for students and teachers, and searches Content with the calling user's own token, so it can never see more than that user.
- Content's indexer and the chatbot call ai-core with Keycloak client-credentials tokens (`content-indexer`, `question-chatbot-agent`) that carry `ai-client` and the audience.
- The `X-Principal` header is removed. Identity always comes from verified JWTs.

**Why.** Folding the harness in keeps the ten-service rule. Searching with the user's own token means the chatbot inherits Content's access check instead of needing its own authority. The chatbot's database is a real persistence need (threads and messages), not symmetry. The chatbot refuses when the best reranked passage is under 0.61. That floor comes from the 2026-10-13 golden run (`rerank-3`, 266 queries): the best refusal floor, 0.613, refused 93% of unanswerable queries and kept 96% of correct answers, with hit@1 0.959, MRR 0.975 and 0 leaks.

**Consequences and limits.**
- The local Postgres image is `pgvector/pgvector:0.8.1-pg17`. pgvector is not a trusted extension, so `scripts/setup.py` creates it as superuser. A volume from the old Alpine image should be recreated or reindexed, because musl and glibc collations differ.
- MinIO images could no longer be pulled anonymously, so local storage is SeaweedFS (`chrislusf/seaweedfs:4.48`), still through the S3 API. The `content-service` identity can only read, write and list the `documents` bucket.
- `.gitattributes` keeps LF endings so `gofmt` passes on Windows. Go services apply numbered migrations once each, recorded in `schema_migrations`, under an advisory lock.
- Service accounts carry a fixed `org_id` locally. Multi-organization deployments need delegated identity (token exchange) before this is production-ready.

## 0003 Per-user daily AI spend caps

**Status:** accepted 2026-10-13 by the project owner, who set the limits: $5 a day per student and $10 a day per teacher. Amended 2026-10-18 by the project owner (timeout charging); amended by 0012 (reservations) and 0023 (headroom, outcome columns).

**Context.** Every chat turn, search and PDF index costs OpenAI and Voyage money. The only limit was Kong's 120 requests a minute shared by every user, so one student could run up the bill and lock everyone out. Users could not see what they had spent.

**Decision.**
- ai-core keeps the ledger, because every paid call passes through it and it knows the model and token counts. Its `aicore` database has one `usage` table: one row per billable call with user, operation, model, tokens and cost. No prompts, answers or business records.
- Callers say who pays: `/v1/inference` and `/v1/rerank` require `billed_to` (`user_id`, `org_id`, `role`); `/v1/ingest` requires `billed_user`, `billed_org`, `billed_role`. Only `ai-client` service accounts call ai-core, taking these from the user's verified token. They decide whose allowance pays, never what a call may read.
- The chatbot bills the user for the rewrite and the answer. Content bills the searcher for the query embedding and rerank, and the uploading teacher for indexing.
- Before a paid call ai-core sums the user's spend since local midnight (`SPEND_TIMEZONE`, default `Asia/Singapore`). At the limit it answers `429 AI_SPEND_LIMIT` with `spent_usd`, `limit_usd` and `resets_at`, and makes no provider call. Limits: `SPEND_LIMIT_STUDENT_USD`, `SPEND_LIMIT_TEACHER_USD`. A token with both roles is refused.
- Prices are a table in `services/ai-core/spend.py` (USD per million tokens), overridable by `MODEL_PRICES`. An unknown model is charged at the most expensive known rate, never free. A stream without usage numbers is charged by estimate (about four characters per token) and marked `estimated`.
- Amended 2026-10-18: a call the provider does not answer in time is charged as if it ran to the end (prompt tokens counted with cl100k_base plus all of `max_tokens`), marked `estimated`.
- No ledger, no call: if the ledger cannot be read, `503 USAGE_LEDGER_UNAVAILABLE` and not ready.
- Users see it: `GET /v1/usage` on the chatbot (`/api/chatbot/usage`), a meter beside the classroom picker, a chat error that says when the allowance resets. Indexing over the limit waits for the reset without using retry attempts.

**Why.** A shared rate limit cannot stop one user's cost from blocking everyone. Putting the ledger in ai-core means no paid call can bypass it. Charging unknown models at the top rate and timeouts in full errs on the side of the payer's limit, not the provider's bill. Prices came from third-party listings checked on 2026-10-13; the provider's own page is authoritative.

**Consequences and limits.**
- The cap is checked before a call and recorded after it, so calls already running can overshoot by what they cost (a few cents to a dollar for a large PDF). 0012 later added atomic reservations.
- Cached query vectors cost nothing and are not charged.
- Spending is per user, not per organization; an organization budget would be a second limit on the same ledger.
- This gives ai-core a database although 0001 and 0002 called it stateless; this record is later and holds.

## 0004 Assessment lifecycle, teacher marking and AI marking suggestions

**Status:** accepted 2026-10-16. The project owner asked for the assessment and grading plan to be implemented without its agent parts, with the recommended answer to each of its five open decisions. Amended 2026-10-17 and 2026-10-18; later amended by 0008, 0010, 0017, 0019, 0020 and 0024.

**Context.** Assessments published at once, held only one-mark multiple-choice and numerical questions, and showed a grade the moment it was finalized. Teachers could not save drafts, set due dates, ask for written answers or give feedback, and no AI helped with marking.

**Decision.**
- Assessment owns the lifecycle. An assessment is `draft` or `published`; drafts can be edited, published or deleted, and students never see them. Optional `opens_at` hides it until then; `due_at` must be in the future at publish.
- Amended 2026-10-18 (owner's request): a published assessment can be replaced (`PUT`) or deleted until a student submits. An unchanged question keeps its id and its saved work. After a submission both are refused with `409 HAS_SUBMISSIONS`; only title, due date and late policy change (`PATCH`). Racing changes and stale submissions get `409 ASSESSMENT_CHANGED`. A published assessment never returns to draft. Deleting a submitted one is allowed since 0008.
- `late_policy`: `accept_flagged` (default, flags `late`) or `reject` (`409 CLOSED`). `release_policy`: `on_finalize` (default) or `manual` (`POST /v1/evaluations/release`). Saved work lives in `saved_answers` until submit.
- Question types, each 1–100 points: multiple choice and numerical marked automatically; short answer with 1–20 accepted answers (trimmed, spaces collapsed, case-insensitive unless set); free response against a rubric of 1–10 criteria, each with 2–6 levels, with an optional teacher-only model answer. A short answer that does not match goes to the teacher, never marked wrong automatically.
- Students never receive answer keys, accepted answers, rubrics or model answers, and never see rubric scores or AI suggestions in their results.
- Grading owns grades. Teacher-marked questions carry `needs_marking`; `POST /v1/evaluations/{id}/marks` records levels or points with feedback. Finalize is refused with `422 NEEDS_MARKING` while any remains.
- `grading.finalized` events carry `awarded` and `possible` and are written at release, not at finalization (corrected 2026-10-17), so Analytics never shows a grade before release. "Correct" means full marks.
- AI marking is a suggestion inside Grading, not a service or an agent. With `ai_marking` set, each question needing marking gets a `marking_jobs` row in the draft's transaction. Workers lease jobs with `FOR UPDATE SKIP LOCKED` and make one structured ai-core `generate` call as `grading-marker`, billed to the classroom's teacher (the creator at first; the current teacher since 0019).
- The prompt treats the answer as data. The reply must use the rubric's own levels, give every criterion once and back any level above the lowest with quotes found in the answer. A hedged answer, or one containing what the scheme rules out, gets the lowest level; a higher level with contradicting quotes is refused as `CONTRADICTED`. Grading adds up points itself, drops feedback repeating the model answer and keeps only known flags. A broken reply is stored as a failed suggestion with its reason, never as a mark.
- The spend limit waits for reset without using an attempt; provider failures retry with backoff up to six attempts; no keys leaves it pending as `AI_PROVIDER_NOT_CONFIGURED`.
- A suggestion never changes a score or reaches a student by itself. Grades stay drafts until the teacher finalizes.

**Why.** The design weighed an agent against a fixed pipeline and chose the pipeline: marking is one bounded judgement per answer, so a loop that chooses its own tools would add cost and risk without adding accuracy. Short answers are never auto-failed because a student may be right in other words. Writing the finalized event at release keeps unreleased grades out of student views.

**Consequences and limits.**
- New Kafka topics for the v2 events and their dead-letter topics; a `grading-marker` client and secret.
- Measuring teacher agreement with suggestions is not built. The second AI opinion came in 0020, the bank and resits in 0017, scheme passages in 0005.

## 0005 Marking-scheme passages as context for AI marking

**Status:** accepted 2026-10-17. The project owner chose the teacher-picked passages option over Grading fetching them at marking time and Assessment fetching them at publish. Partly superseded 2026-10-18 by 0009: schemes are no longer uploaded or searched; passages stay as marking points the teacher writes, and the event and Grading parts still hold.

**Context.** The marker read only the question, rubric or accepted answers and the model answer. Grading's marker runs in the background as `grading-marker` with no teacher token, so it cannot search Content with a user's token and must not read Content's database.

**Decision.**
- A short answer or free response may carry `marking_scheme`: up to 5 passages, each 1–2,000 characters, at most 20 per assessment. Other types refuse it. A passage may name `document_id`, `chunk_id` and `page`, as references only; no service fetches by them.
- Passages are part of the question: saved on the draft, frozen at publish, never in the student view.
- `assessment.submitted.v3` carries them (v2's schema forbade extra fields). The topic carries teacher-only text and needs the same production ACLs as the answer keys already in it.
- The marking prompt adds the passages as the teacher's own scheme, used with the rubric, with the rubric's levels deciding disagreements, never quoted to the student. Feedback repeating 25 or more characters of a passage is withheld. A suggestion records `scheme_passages`.
- (Retired by 0009: picking passages by searching an uploaded scheme from the editor.)

**Why.** Rejected alternatives: Grading searching Content at marking time would need a new kind of service authority over teacher-only data, pay for a search per job, and could mark two students against different passages after a re-upload. Assessment searching at publish means the teacher never reviews what the AI reads, and couples Assessment to Content and ai-core.

**Consequences and limits.** A marking call grows by up to 10,000 characters of context per question. No migration: questions are JSONB.

## 0006 Question generation and drafted marking guidance

**Status:** accepted 2026-10-17. The project owner asked for the question generation plan to be built, with these decisions: teachers only, no numerical questions in the first version, and generation before a question bank. Amended 2026-10-24 (counts); amended by 0009, 0015, 0022 and 0026.

**Context.** Teachers wrote every question, accepted answer and rubric by hand, even with the classroom's PDFs uploaded. The question-generation agent was a 501 placeholder.

**Decision.**
- The agent is a fixed, stateless pipeline at `/api/question-generation`, called with the teacher's own token. Only the classroom's teacher may call it (checked at Identity); others get 403. Students use a separate route (0022).
- `POST /v1/questions` takes a classroom, 1–10 documents, one topic, 1–3 types (multiple choice, short answer, free response), a difficulty, a count per type (1–10 each, at most 10 in total, amended 2026-10-24), an optional focus and the editor's prompts. It returns drafts in Assessment's question shape with `requires_teacher_review: true`. Nothing is saved.
- `POST /v1/marking-guidance` drafts accepted answers or a rubric and model answer, plus marking points, for a question the teacher wrote.
- Each request reads the topic from Curriculum and searches Content with the teacher's token over teaching material only (12 passages for questions, 6 for guidance). Below `RELEVANCE_FLOOR` (default 0.5) the request fails `422 INSUFFICIENT_SOURCE`. If reranking fell back to unscored order, `503 RERANK_UNAVAILABLE`. Calls go to ai-core as `question-generation-agent`, billed to the teacher.
- Every draft is checked: it must cite given passages and quote at least 20 characters of evidence found in one. Multiple choice needs 2–8 distinct options with the key among them; short answer 1–20 accepted answers; free response a valid rubric. Near-duplicates are dropped. The response reports requested, returned and dropped by reason; `422 GENERATION_REJECTED` or `GUIDANCE_REJECTED` when nothing passes. Failures are never padded.
- Generated short answers and free responses carry their marking points as one passage. Questions gain optional `sources` (up to five) and `origin: "generated"`, teacher-only and left out of the submitted event.
- The editor lists drafts with key, evidence and sources; "Add to assessment" makes an ordinary editable question. The whole scheme PDF is never sent to a marking call.

**Why.** Refusing below a relevance floor avoids generating from too little source. Quoted evidence and teacher review keep generated questions tied to the materials and under the teacher's control. The 0.5 floor was calibrated on the eval corpus on 2026-10-19 with `rerank-3`:
- Topics their documents cover scored 0.73–0.94, and teachers' questions 0.50–0.94.
- Unrelated subjects scored 0.27–0.36, and uncovered topics mostly 0.32–0.51.
- At 0.5 it kept 46 of 46 covered topics and 39 of 40 questions. It refused 24 of 27 unrelated or uncovered topics, and 39 of 40 questions asked of the wrong document.

**Consequences and limits.**
- A course schedule page that lists every topic can lift an uncovered topic above the floor; the drafting checks then reject what the model writes.
- The floor still needs checking against real classroom documents, beyond the eval corpus.
- Without provider keys: `503 SEARCH_UNAVAILABLE` or `AI_PROVIDER_NOT_CONFIGURED`.
- Not built: numerical questions, and measuring how many generated questions teachers keep. The bank (0017) keeps `origin` but not `sources`.

## 0007 The curriculum is a flat list of topics

**Status:** accepted 2026-10-18. The project owner decided to cut the unused curriculum levels ("Cut the levels"), including the destructive migration this needs, and then to call the one remaining level "topic" everywhere. Amended 2026-10-19 (old event versions retired).

**Context.** Curriculum stored a six-kind tree (subject, course, unit, topic, skill, learning objective), but only skills were used anywhere. Teachers still had to choose a level and parent, and archiving was refused while a node had children.

**Decision.**
- A topic is `{id, name}`, no kind or parent. Prerequisites link topics. API: `GET/POST /v1/topics`, `GET/PATCH/DELETE /v1/topics/{id}`, `POST /v1/topics/{id}/prerequisites`, `GET /v1/topics?archived=true`, `POST /v1/topics/{id}/restore`. `/v1/nodes` is removed.
- "Skill" is gone from new contracts: `topic_id`, `UNKNOWN_TOPIC`, `INVALID_TOPIC`, `TopicMetric`.
- New event versions: `assessment.submitted.v4` and `grading.finalized.v3`. Amended 2026-10-19: with every stack upgraded and Kafka keeping these topics 7 days, older versions were retired; Grading reads only v4, Analytics only v3.
- Each owner renames its own data. Curriculum `002_topics.sql` (destructive, authorized 2026-10-18) deletes non-skill nodes and their links; skills keep their ids. Assessment, Grading and Analytics rename stored keys in their own `004_topics.sql`.

**Why.** Nothing read the levels above skill, and having both "topic" and "skill" name the same thing was confusing.

**Consequences and limits.** Subjects, courses, units and objectives created before 2026-10-18 were deleted and cannot be recovered; nothing outside Curriculum referred to them. Grouping topics can return later as an additive change.

## 0008 Deleting assessments students have submitted

**Status:** accepted 2026-10-18. The project owner asked for teachers to be able to delete an assessment after students have submitted it, together with its grades.

**Context.** A submitted assessment could not be deleted (`409 HAS_SUBMISSIONS`). Its data is spread across Assessment (submissions), Grading (evaluations, audit) and Analytics (evidence), and no service may touch another's database.

**Decision.**
- `DELETE /v1/assessments/{id}` still answers 409 once anyone submitted. Repeating it with `?with_submissions=true` deletes it with its submissions and saved work. Only the classroom's teacher may delete.
- For a published assessment, Assessment writes `assessment.deleted.v1` to its outbox in the delete's transaction and drops its unsent events for it. A draft writes nothing.
- Grading deletes the evaluations, audit, marking jobs and unsent events, released grades included. Analytics deletes evidence and evaluations. Each keeps `deleted_assessments` so a late event is ignored. Grading's purge takes an exclusive assessment lock that its insert takes shared; Analytics uses its classroom lock.
- Repeats change nothing; malformed events go to `assessment.deleted.v1.dlq`.
- The dialog is titled "Delete this assessment and its grades?", counts the submissions, and focuses Cancel first.

**Why.** Teachers who published by mistake had no way to remove an assessment. An event lets each owner purge its own data without crossing database boundaries.

**Consequences and limits.** Cannot be undone; students lose those results. Not purged: ai-core's usage records (billing) and event copies on Kafka until retention ends (7 days since 0010).

## 0009 Retire uploaded marking schemes, and let teachers delete materials

**Status:** accepted 2026-10-18. The project owner found the marking-scheme workflow finicky and chose to drop it: teachers write how a question is marked themselves (or keep what AI question generation drafts), and assessments can still be generated. In the same change, teachers can delete an uploaded material. Supersedes the upload, search and matching parts of 0005 and "Match scheme to questions" in 0006.

**Context.** Matching a scheme's passages to questions was unreliable and hard to check, while the per-question rubric, accepted answers and model answer already carry what AI marking needs. Teachers could not remove a document without deleting the classroom.

**Decision.**
- `POST /v1/documents` refuses `rubric` and `marking_scheme` with 422; only `teaching_material` is accepted. Earlier uploads keep their type, stay teacher-only and can be deleted. No migration.
- `/v1/ingest` has one chunking path, with no `layout` parameter.
- Marking points stay (up to five per question, 20 per assessment), written by the teacher or kept from drafted guidance, and still travel on `assessment.submitted.v3`. Generation searches with `doc_types: ["teaching_material"]`.
- `DELETE /v1/documents/{id}` (teacher, 204) removes the row (and with it chunks, vectors and indexing job) and writes `content.deleted.v1` in the same transaction. Object removal became a durable job in 0012. The chatbot consumes the event since 0010.

**Why.** Rejected alternatives: fixing matching (every fix still left the teacher checking each passage, the work matching was meant to save); dropping marking points too (AI marking would lose its notes, and the event, Grading and stored questions would change for no gain); converting legacy schemes to teaching material (students would see answer keys).

**Consequences and limits.** Questions keep passages they copied. The retrieval eval drops the rubric and scheme from its corpus; the student leak checks that name them stay.

## 0010 No stale data after a removal or deletion

**Status:** accepted 2026-10-18. The project owner asked for the findings of the stale-data audit to be fixed. The 7-day retention was confirmed by the project owner on 2026-10-19. Amended 2026-10-18 and twice on 2026-11-20.

**Context.** An audit found data outliving the action that should end it: an unenrolled student's drafts stayed in review queues and their AI marking kept billing the teacher; a deleted material stayed readable in chat citations; Kafka kept submissions (with keys) and grades 30 days and outboxes kept events forever; a due-date change left `late` flags; prerequisites could not be read or removed; objects with no committed row stayed in the bucket.

**Decision.**
- Identity writes `identity.enrollment.removed.v1` in the removal's transaction. Grading records it in `removed_enrollments`; a marking job for that student ends `STUDENT_REMOVED` with no model call. Submissions and grades are kept.
- Amended 2026-11-20: enrolling writes `identity.enrollment.added.v1`. Both events carry Identity's database clock, so removals and returns order correctly. Grading keeps each student's newest enrolment in `enrollment_returns`; a return queues again the suggestions that ended `STUDENT_REMOVED`, and the marker re-checks enrolment before any model call. A first enrolment costs Grading nothing.
- Teacher lists and counts in Grading and Assessment read the roster from Identity with the teacher's token and count only enrolled students. Amended 2026-10-18: a release applies only to enrolled students.
- The chatbot consumes `content.deleted.v1`, keeps `deleted_documents`, and blanks every citation of the document (`removed: true`) under a lock that saving an answer also takes. Redacted citations are not fed back as history.
- Submission, finalized-grade and dead-letter topics keep 7 days (`retention.ms=604800000`); other topics keep the broker's 30. Each outbox prunes events published more than 7 days ago, in batches of 1000 (at most ten seconds a minute, amended 2026-11-20); unpublished events are never pruned.
- A due-date `PATCH` recomputes `submissions.late` and writes `assessment.due_changed.v1`; Grading recomputes `evaluations.late`.
- `GET` and `DELETE /v1/topics/{id}/prerequisites[/{prerequisite}]` are added.
- Content hourly removes objects older than 6 hours that no row names, skipping documents still indexing.
- Amended 2026-11-20: purges find outbox rows through partial expression indexes built concurrently (`platform.IndexBuilder`), so a build never blocks outbox inserts.

**Why.** Each fix closes a specific audit finding. Seven days covers delivery and recovery while limiting how long answer keys and grades sit on the broker.

**Consequences and limits.** An Analytics rebuild from Kafka now reaches back 7 days, not 30. Removals made before this change were never announced, so marking jobs already queued for them are not stopped. Test-only cleanup in `scripts/local_stack.py` still deletes topics directly.

## 0011 Users can delete their own account

**Status:** accepted 2026-10-18. The project owner asked for account deletion through a simple Profile tab. Keeping ai-core's usage ledger was confirmed by the project owner on 2026-10-19.

**Context.** Nothing erased a user. Identity's `users` row cannot be removed, because a deleted classroom keeps its `teacher_id`.

**Decision.**
- `DELETE /v1/me` (204; 404 when already deleted) in one transaction: lock the row, refuse a teacher who owns a live classroom (`409 OWNS_CLASSROOMS`), delete enrollments, set `deleted_at`, clear `username` and `display_name`, and write `identity.user.deleted.v1`. A deleted account cannot register (`403 ACCOUNT_DELETED`), be enrolled or create a classroom.
- A teacher deletes classrooms first, since a classroom delete already purges everything in it with its own warning.
- Assessment, Grading, Analytics and the chatbot each purge the user's data in the consumer's transaction, under an exclusive `user` lock their inserts take shared, and keep `deleted_users` so a late event cannot bring data back.
- Identity removes the Keycloak user every 15 seconds for pending accounts, through an `identity-accounts` client holding only `realm-management/manage-users`. The web app signs the user out at once.
- ai-core's ledger is kept: it is the billing record and holds no answers or content.

**Why.** Deleting classrooms first stops a teacher from wiping students' work without the classroom warning. Per-service purges keep database ownership.

**Consequences and limits.** A token issued before deletion stays valid until it expires. Event copies remain until Kafka retention ends. An administrator cannot delete another user, and deletion cannot be undone. An admin cannot delete their own account (0019).

## 0012 Efficient retrieval and durable cleanup

**Status:** accepted 2026-10-19. The project owner authorized all eight improvements from a review of retrieval and cleanup, including PDF processing.

**Context.** Not stated beyond the review's findings.

**Decision.**
- Content deletes records and writes the deletion event and an object-cleanup job in one transaction; its worker retries object deletion after commit. Classroom purges use the same queue. Originals are never removed before the database delete commits.
- ai-core examines PDF text and layout locally first. Clean text uses the parser; scanned pages, complex graphics and uncertain tables keep selective vision. Full vision stays available for comparison and recovery.
- Caches reuse versioned extraction and chunking and exact embedding inputs. Misses and failures never manufacture output. Identical concurrent requests share work in one process.
- Chat skips the rewrite for unambiguous standalone questions, keeps conversation-aware rewriting and topic coverage, and uses a token budget for evidence. Thresholds are not applied to incomparable RRF scores.
- ai-core reserves estimated spend atomically before provider work, settles actual usage after, and enforces concurrency across replicas. No transaction stays open during a provider call. Abandoned reservations expire conservatively.
- Optional `AI_TOTAL_SPEND_LIMIT_USD` sets a ceiling across all users with no daily reset. The owner authorized at most $5 across this verification run; its isolated ledger used a $4 ceiling with margin.
- Lists gain bounded pagination and batch reads without widening authorization. Pool limits become validated configuration. Metrics never use prompts, tokens or student identities as labels. No new service, Redis, sharding or framework.

**Why.** Reservations close the overshoot 0003 accepted. Durable cleanup means a crash cannot leave an orphan or lose an original before commit. The record gives no further reasons.

**Consequences and limits.** Physical object cleanup is asynchronous and retryable. Cache versions must change when extraction semantics change; caches are optimizations, not authorities. Benchmarks describe the machine and corpus measured and do not establish production capacity.

## 0013 Native structured documents

**Status:** accepted 2026-10-21. The owner authorized native heading-aware handling of Markdown, PPTX and DOCX, and deferred PDF improvements and test execution.

**Context.** Only PDFs and plain text were indexed; structured formats lost their headings.

**Decision.**
- Content accepts `text/markdown` (5 MiB) and PPTX and DOCX (50 MiB), and queues the existing indexing worker with the MIME type. `.txt` and PDF handling are unchanged.
- ai-core reads structure locally, with no Office execution, converters or model extraction. Markdown: headings, code, lists, pipe tables. PPTX: slide order, titles, body, tables and speaker notes (the upload form says notes are included). DOCX: outline levels, heading styles, body, lists, tables; repeated headers and footers are not headings.
- Each section or slide is a chunk boundary. Heading ancestry is stored in `section_path` and included in embedding and rerank inputs. Uncached embedding work goes through ai-core billing.
- `page_start`/`page_end` stay numeric: slides for PPTX, logical section anchors for Markdown and DOCX, not rendered pages. Citations carry optional `section_path`.
- Bounds: archives over 4096 entries or 64 MiB expanded, XML parts over 8 MiB, extracted text over 8 MiB, duplicate entries, entity declarations and relationship traversal are refused. Chunk content is capped at 20,000 characters, heading paths at 20,000, each embedding input at 2,200 tokens. External resources are never fetched; no macros run.

**Why.** The 2,200-token cap keeps the existing 128-input embedding batches bounded. Oversized structure fails before embeddings rather than silently truncating headings.

**Consequences and limits.** Images, charts, SmartArt, equations, merged-table layout, text boxes and full Markdown dialects are not interpreted. Nothing is reindexed automatically. Tests were added but the owner deferred running them, so they are not passing evidence; no paid eval was run.

## 0014 Performance interpretation agent

**Status:** accepted 2026-10-23. The project owner asked for the performance interpretation agent to be built as the first agentic workflow, keeping clear of the retrieval work other contributors own. Amended 2026-10-27 (reference schema).

**Context.** Teachers saw topic accuracy per student but had to work out what was going wrong themselves. Unlike earlier fixed pipelines, which answers are worth reading depends on what the summary shows, so the model should choose what to look at.

**Decision.**
- A bounded, stateless tool-using loop at `/api/performance-agent`, called with the teacher's own token. `POST /v1/performance_interpretation` takes a classroom and student; only the classroom's teacher, for an enrolled student. The agent gathers its own evidence, so a caller cannot supply invented data.
- It opens with the student's released topic summary (Analytics), finalized evaluations (Grading), the teacher's assessment view and topic names. No finalized results: `422 NO_EVIDENCE` with no model call. Drafts and AI suggestions are never shown to the model.
- Up to five turns (`PERFORMANCE_MAX_TURNS`), each one structured ai-core call billed to the teacher. A turn calls up to three read-only tools or reports. After two minutes (`PERFORMANCE_DEADLINE`) it may only report; the whole request ends within four minutes (`PERFORMANCE_BUDGET`), inside Kong's five.
- Tools: `read_answers` (one result's questions, lost marks first, at most eight), `prerequisites`, `class_comparison` (the first 200 classmates in roster order, with a partial flag).
- The model uses short references (`R1`, `E1`, `T1`), never ids. Amended 2026-10-27: each reply schema lists only question references as evidence and topic references as recommendations (up to 250 each).
- Checks: every observation cites a question shown; a possible misconception cites only questions that lost marks and whose answers were read; recommended topics were shown. At most five observations, three misconceptions, four interventions, three topics. Nothing left: `422 INTERPRETATION_REJECTED`. Failures are dropped and counted, never repaired. Marked `requires_teacher_review`; nothing saved.

**Why.** Marks alone are not enough to name a wrong idea, so misconceptions need read answers. References stop the model writing ids. The schema change came after the agent eval found strong students' reports refused because every observation cited `R` or `T` references.

**Consequences and limits.**
- Up to five model calls per interpretation, bounded by the turn limit, the deadline and tokens per turn, each against the teacher's allowance.
- Without provider keys: `503 AI_PROVIDER_NOT_CONFIGURED`.
- The summary uses released grades and the results include unreleased finalized ones, so they can differ under manual release.
- Upstream responses are validated into typed views; numeric strings and boolean counts are rejected before any model call. Topic names are read in batches of up to 50.
- The agent does not search Content or call the chatbot.
- Not built: automatic runs on finalization (it would need storage and a payer), saving, linking to teaching material, practice from recommended topics, and evaluating reports against teachers' judgement.

## 0015 Question generation critic loop

**Status:** accepted 2026-10-24. The project owner asked for the third agentic workflow. It amends 0006. Amended by 0022 and 0026.

**Context.** 0006's checks catch missing quotes, bad keys and repeats, but not whether a question is answerable from its passages, whether the key is right, or whether wrong options are too obvious. A dropped draft was simply lost.

**Decision.**
- A reviewer sees each passing draft's prompt, options and cited passages only, not the generator's explanation or evidence, and works out the answer itself. It judges answerability, key, single right option, plausible wrong options and repeats.
- A failed draft goes back with the reason, up to two retries (`QUESTION_REVIEW_RETRIES`); a retry asks only for each type's shortfall. At most six model calls (three writes, three reviews), billed to the teacher.
- `QUESTION_REVIEW_DEADLINE` defaults to 240 seconds (clamped 30–240). A retry starts only if time so far plus 1.5 times the longest round fits. A call with under 5 seconds left is not started. Kong's and the web client's timeouts are five minutes.
- Verdicts fail closed: an unreadable reply, a missing verdict or an unknown reason never counts as a pass. A draft with no usable verdict comes back `not_reviewed`. When the reviewer cannot run, passing drafts return `not_reviewed`. Nothing is marked passed without a review.
- A weak rewrite is matched to its original at prompt similarity 0.75 (`REWRITE_RATIO`, looser than the 0.9 duplicate check) so both are never returned.
- Reason codes: `NOT_ANSWERABLE`, `WRONG_KEY`, `AMBIGUOUS`, `WEAK_DISTRACTORS`, `REPEATS_EXISTING`, `NEEDS_MATERIALS`, `NOT_REVIEWED`. A fixed phrase list ("according to the passage(s)", "in the passage above", "the examples above", "the described" and a few more) catches questions that lean on the materials without a model call. It is narrow on purpose so "the passage of a law" or "described as" pass; slide phrases were removed because they match ordinary questions.
- `LONG_KEY`: a key of at least 40 characters and over 1.8 times the average of the other options, checked after the reviewer passes the draft. Flagged notes carry `source` (`reviewer` or `check`).
- Weak-option drafts are shown flagged rather than lost. Since 0026's amendment a teacher's draft failing only on weak options is not rewritten.
- `review: false` per request, or `QUESTION_REVIEW=off`, skips the review.

**Why.** Evidence from real-key runs on 2026-10-24 (software-testing slides):
- First comparison (5 multiple choice, 3 short answer, Intermediate): both runs returned 8 of 8; review 2 calls against 1, each under $0.01. The reviewer rejected nothing but missed questions leaning on the materials, giveaway options and the same fact in two types. The phrase list, `LONG_KEY` and the wider `WEAK_DISTRACTORS` and `REPEATS_EXISTING` rules came from that run.
- Second run after tuning (Advanced; 4 multiple choice, 2 short answer, 2 written): 8 of 8 in one round, 2 calls, about 19 seconds, cost not recorded. Still seen: "described as optimized for Vite", a source error passed through, weak distractors.

**Consequences and limits.**
- Up to six calls instead of one, and slower; the page says so while it loads and offers "Skip the review".
- The response reports each round (written, passed the checks, passed the reviewer, rejected by reason, `reviewed`, `retried`, `sent_back`) and the call count. The page shows one line per round under "How the drafts were checked".
- When nothing is kept and a retry stopped on an upstream error, the teacher gets that error (429, 502 or 503); `422 GENERATION_REJECTED` is kept for when every round ran and nothing passed.
- The reviewer checks against the passages, not the subject, so a mistake in the materials passes (a stub described as a "simulated caller", which is really a driver). The page's caption says so.
- `model_calls` counts requests attempted, including failures and refusals before dispatch; money is authoritative only in ai-core's ledger.
- Not built: extra searching when a draft is sent back. Guidance review and the check profile came in 0026.

## 0016 Reopening finalized grades and finalizing marked grades together

**Status:** accepted 2026-10-26. The project owner asked for a review's findings to be fixed, including "finalized grades can never be corrected" and "each finalize needs a typed reason".

**Context.** A finalized grade was permanent, and Analytics ignored later events for an evaluation. A teacher finalizing an auto-marked quiz for 30 students needed 30 dialogs and 30 reasons.

**Decision.**
- `POST /v1/evaluations/{id}/reopen` with a `reason` (1–1000 characters), for the classroom's teacher while the student is enrolled. In one transaction the grade becomes a draft, `revision` goes up by one, and the audit gets `action='reopen'`. Marks and feedback are kept; no new AI suggestion is requested. The student does not see the draft.
- `grading.finalized.v3` gains optional `revision`. A reopened released grade writes `grading.reopened.v1`. Analytics keeps each evaluation's newest revision and whether it is withdrawn: older events change nothing, a newer finalized event replaces the evidence, a newer reopen removes it.
- `POST /v1/evaluations/finalize` with `{assessment_id, reason?}` finalizes every draft of enrolled students with nothing left to mark, each with its own audit entry. `GET /v1/evaluations/counts` adds `ready`.

**Why.** Revisions give Analytics the newest released evidence whatever order the two topics arrive in, because Kafka does not order one topic against another. A replay from the start reaches the same state.

**Consequences and limits.** A correction reaches Analytics as a withdrawal and a new finalization, so evidence briefly leaves performance views. Analytics must deploy before Grading, or it dead-letters events carrying `revision`. A finalized grade still cannot be edited in place.

## 0017 Question bank and resits

**Status:** accepted 2026-11-01, after five independent reviews the same day. The project owner asked to plan the question bank and resits, which 0004 lists as not built yet and the brief gives to Assessment ("Question banks", "Assignments"). Amends 0004 and 0006. Amended 2026-11-22 (changing a resit's students).

**Context.** A question existed only inside its assessment, and each student could submit once, so a second attempt meant a new assessment for the whole class.

**Decision.**
- Each teacher has one bank in Assessment: questions without id, `sources` or `marking_scheme`, validated and topic-checked like assessment questions. Only the owner can reach it. Up to 2,000 per teacher (`409 BANK_FULL`), under an exclusive per-teacher lock. Cross-service checks run before any lock is taken.
- The bank holds no passages, because passages quote one classroom's documents. A resit copies passages from its original, in the same classroom.
- Bank and assessments hold copies. A link table outside the question names the bank question it came from, so the question JSON and `assessment.submitted.v4` are unchanged. Links are plain references; saving an already-linked question is `409 ALREADY_IN_BANK`.
- `POST /v1/assessments/{id}/resits` makes a draft in the same classroom with `resit_of`, 1–100 assigned students on the roster and 1–50 questions from the original and the bank. It copies late and release policies, AI marking only when a teacher-marked question remains, and no dates. Only the server sets `resit_of` and assignees.
- Only resits are assigned. An unassigned student gets the same 404s as for an unopened assessment on every path. Students see `resit: true` but never the bank.
- `PUT /v1/assessments/{id}/assignees` replaces the list: not empty, added students enrolled, submitters cannot be removed (`409 HAS_SUBMISSIONS`), removal deletes saved answers; submit and autosave re-check under the row lock.
- The original grade is never changed; both grades stay. "Best of" is not part of this.
- The bank list filters by topic, type, text and origin, and marks questions a chosen student already answered in that teacher's classrooms. This reaches past the current roster on purpose: it is the teacher's own past assessment, shown only as a hint. The resit builder suggests students and topics from finalized grades, with no model call.
- Amended 2026-11-22: a "Change students" dialog on the editor and resit rows uses the shared roster picker (twenty a page, up to 100 chosen). Taking anyone off a published resit asks first. Learning performance (0014) links "Create resit for this student" per cited assessment.

**Why.** Copies mean a published or submitted assessment is never edited behind the teacher's back. Keeping passages out of the bank means no classroom's material lives outside that classroom, and a classroom delete needs nothing from the bank. A resit for more than 100 students is better given as a new assessment.

**Consequences and limits.**
- No new service, topic or event version; Grading grades a resit as it is. Migration `006_bank_and_resits.sql` is additive, and `assessments.assigned` defaults to false, so every existing assessment stays for the whole class.
- Purges: a deleted teacher's bank questions go with their account; a deleted student's assignee rows go with their work; a classroom delete removes its assessments with links and assignees, while the bank stays. Bank and assignee writes check `deleted_users` under the user lock, so nothing lands after a purge.
- Teacher summaries gain `resit_of` and `assignee_count`; the class dashboard counts submissions out of `assignee_count` when set.
- A bank question has no marking points, so AI marking relies on rubric and model answer.
- Assessment does not consume `content.deleted.v1`, so passages already in assessments keep a deleted material's text (teacher-only, a known gap the bank does not widen).
- Analytics' "Needs practice" flag reads a topic's latest ten questions, so a resit lifts it only once its grade arrives after the original's and outweighs the earlier wrong answers.
- Self-practice came in 0022.

## 0018 Platform monitoring and operator access

**Status:** the record gives no accepted date or approver. Access partly superseded 2026-11-19 by 0019: the school's `admin` role replaces `platform-operator`, and spend reports are limited to the admin's own organization. The collector and its reads still hold.

**Context.** Operators need one view of service readiness, request latency, dead-letter retention and recorded AI spend, without changing the ten applications' responsibilities or databases.

**Decision.**
- The collector is stateless operational infrastructure beside Kong and Kafka, not an eleventh application. It probes the ten applications privately, reads dead-letter partition offsets, and calls ai-core's internal spend API. It makes no model calls and keeps rolling samples in memory. ai-core keeps sole ownership of the ledger.
- Kong exposes three read operations under `/api/monitoring`. They need a validated JWT and, since 0019, the `admin` role. A `monitoring-reader` client reads private latency and spend with client credentials.
- Spend reports are `Cache-Control: no-store`, and each served report is logged with the subject and correlation id.
- Each process supplies its latency bucket boundaries; the collector accepts samples only when they match its schema.

**Why.** Keeping the collector stateless and outside the ten applications preserves service ownership.

**Consequences and limits.** The role and reader secret need provisioning. ai-core builds its date index concurrently so ledger writers keep running during an upgrade. Producers without the boundaries field report latency as unavailable until replaced.

## 0019 School admins manage accounts and classrooms

**Status:** accepted 2026-11-19 by the project owner, who chose one per-school admin role in place of the platform operator, admin-created accounts, one teacher per classroom and read-only rosters for teachers. Supersedes 0018's operator access and the teacher's classroom management.

**Context.** Teachers created classrooms and enrolled students; accounts existed only in Keycloak, made by scripts. A cross-organization `platform-operator` role could open monitoring. Schools want one person to set up the year, and no role that sees across schools.

**Decision.**
- Realm role `admin` belongs to one school through `org_id`. An account holds exactly one of `admin`, `teacher`, `student`; more is `403 ROLE_CONFLICT`.
- Identity's `/v1/admin/` operations are limited to the admin's organization; another school's classroom is 404.
- Accounts: list by role; create through `identity-accounts` with the school's `org_id`, the role and a random 128-bit temporary password with `UPDATE_PASSWORD`. The username is `org_id` + "." + the chosen name (e.g. `local-school.ada`), so `409 USERNAME_TAKEN` reveals nothing about other schools. A failed record removes only its own Keycloak user (tagged with `identity_request`). Passwords are answered once with `no-store`, never stored or logged. `POST .../password` resets one.
- `org_id` is `ADMIN_EDIT` in the user profile, so users cannot change it.
- Classrooms: `GET/POST/PATCH/DELETE /v1/admin/classrooms`. One live teacher each. Reassignment emits `identity.classroom.reassigned.v1`, ordered by database time under the row lock; Grading then bills AI marking to the new teacher, drafts included. Banks stay personal.
- Enrolment by account id or username. Teachers keep their classroom list, `/access`, the roster and `/student-ids`, and lose create, delete and enrol.
- Admins get no classroom relation, so they see no materials, grades or chats. An admin cannot delete their own account (`403 ADMIN_ACCOUNT`).
- Monitoring requires `admin` and forces spend reports to the admin's `org_id` (another is 403). Readiness, latency and dead letters stay as they are.

**Why.** The owner's choices above. Prefixing usernames with the school keeps realm-wide uniqueness from leaking other schools' names.

**Consequences and limits.**
- `scripts/upgrade_local.py` creates the `admin` role, grants `view-realm` to `identity-accounts`, sets the attribute policy, makes email optional (created accounts have none) and creates the local school's admin from `ADMIN_PASSWORD`.
- `identity.classroom.reassigned.v1` is a new topic, and Grading applies the additive `009_classroom_teachers.sql`; Grading deploys with or before Identity.
- `platform-operator` is left in place, unused. Old accounts such as the local `teacher` and `student` keep their names; new ones cannot take them, since every new username starts with the school's id.
- Identity creates sign-in accounts but does not yet disable or rename them; admin accounts are still provisioned in Keycloak.

## 0020 Marking critic

**Status:** accepted 2026-11-21. The project owner asked for the second agentic workflow. It amends 0004. Amended by 0024 (profile, re-ask, exact match).

**Context.** The marker's suggestion carried only the model's own `confidence`, so a confidently wrong suggestion looked the same as a sound one.

**Decision.**
- When a suggestion is `ready`, its job becomes a review job in the same transaction (`stage` `review`, migration `011_marking_review.sql`) and the suggestion gets `review: {status: "pending"}`. Failed suggestions are not reviewed.
- The critic is a second ai-core call (`marking.review`), billed to the same teacher, on the `check` profile since 0024. It gets the same inputs under its own prompt (a strict, independent second examiner who chooses the lower level when unsure) and never sees the first suggestion. Its reply passes the same checks.
- `compareSuggestions` sets `doubtful` on differing levels (`LEVELS_DIFFER`), low confidence in either (`LOW_CONFIDENCE`) or a new known flag (`CRITIC_FLAGGED`); otherwise `agreed`. The critic's feedback to the student is dropped.
- Fails closed: a bad reply, failed call or exhausted attempts give `not_reviewed` with a reason, never `agreed`. Other endings without a call: `FINALIZED`, `STUDENT_REMOVED` (re-queued on return), `MARKED_BY_TEACHER` (the teacher marked otherwise), `NO_TEACHER`, `CRITIC_OFF`, and `EXACT_MATCH` (0024). A teacher who saved the suggestion's own marks still gets the check.
- The review never changes a suggestion or mark. Students never receive it. The teacher still finalizes. `MARKING_CRITIC=off` turns it off.
- `/v1/evaluations/counts` adds `ready_doubtful`. The review page shows "Second check agrees." or "Second check disagrees: …", a "Check AI mark" chip, and sorts doubtful drafts first when the page loads; finalize dialogs warn without blocking.

**Why.** A second, blind marking makes a confident error visible. Blindness keeps the critic from anchoring on the first answer. A teacher who accepted the suggestion's marks is checked because that is when a wrong suggestion costs most. Evidence is in 0024.

**Consequences and limits.** Two model calls per ready written answer instead of one. A Grading built before this change would run a review job as a new suggestion, so old instances must stop before the new one starts. Not built: consistency across similar answers, teacher-agreement measurement.

## 0021 Class-insights copilot

**Status:** accepted 2026-11-21. The project owner asked for workflow 5, the teacher class-insights copilot, to be built: a teacher asks one question about a class and gets one checked answer, without searching teaching material.

**Context.** Nothing answered class-level questions such as "who is struggling with recursion, and what should I reteach?". Answering means choosing which topics, questions and students to look at, the same shape as 0014. The ten-application rule rules out a new service.

**Decision.**
- A second route in `student-performance-agent`: `POST /v1/class_insights` with a classroom, a question (1–500 characters) and up to three earlier questions, for the classroom's teacher, through the existing Kong route. Stateless; earlier questions are context, never evidence.
- The turn loop moves into the service's `loop.py` and both agents share it. It stays in the service, not in `learning_transport`, because both users are there.
- It opens with released class metrics (first 200 students in roster order), roster names (up to 500) and topic names. No released evidence: `422 NO_EVIDENCE`.
- Up to five turns under 0014's settings, schema names `class_insights.turn` and `class_insights.report`. Tools: `topic_detail`, `topic_questions` (up to five pages of 100 evaluations, flags `older_results_not_read`), `student_results` (up to 20 results), `read_answers` (at most eight), `prerequisites`.
- Each tool turn lists `suggested_checks`: `topic_questions` on every topic at least two students struggle with, then `read_answers` on its worst question.
- Names never reach the model. It uses `S1`, `T1`, `Q1`, `R1`, `E1`; names are added after the checks, for cited students only.
- Checks, dropped and counted, never repaired: a struggling student needs `observed` evidence on the topic and the needs-practice flag or under 60% of the marks (`INSUFFICIENT_EVIDENCE`, `NOT_SUPPORTED`). Observations, misconceptions and reteach items must cite what was shown. A text that writes a reference (`S14`, `T2`) is dropped (`REFERENCE_IN_TEXT`); text that only looks like one, such as an assessment titled "T9 quiz" in a four-topic class, is kept.
- Every percentage ("73%", "73 percent") must be, within 0.5 percentage points, a share the model was shown for what the item cites (`UNSUPPORTED_FIGURE`; a struggling group with one is dropped whole). The allowed shares, for each cited topic and the prerequisites shown for it, are:
  - the class's share of the marks, and the share of students with evidence who struggle;
  - each cited student's share of the marks and recent marks share;
  - for each question row shown, the share of answering students who lost marks and the mean marks lost over possible, and the complement of each;
  - each cited answer's own marks.
- At most six struggling groups, five observations, three misconceptions and four reteach items are kept, 500 characters each. Nothing left: `422 INSIGHTS_REJECTED`, or `422 NOTHING_TO_ANSWER` when the model reported nothing at all. The response is marked `requires_teacher_review` and nothing is saved.
- Step reasons replace references with names or titles, for both agents.

**Why.** Reusing the interpretation agent's loop and service keeps the ten-application rule. Rules added 2026-11-21 from eval runs: `suggested_checks` and evidence-describing texts after the first run left out a student weak on every topic and wrote "listed as struggling"; the reference rule after the 14-student class drew "S14 is the only student…" in three runs out of three; the percentage check so figures are checked rather than trusted.

**Consequences and limits.** Up to five model calls per question. `scripts/eval/run_class_insights.py` measures two scripted classes and can repeat cases for stability. Released and finalized figures can differ under manual release. A class over 200 students is answered from its first 200, and the scope says so. Not built: saved threads, material search, looking a student up by name.

## 0022 Student self-practice

**Status:** accepted 2026-11-22. The project owner asked for students to practise from their class's materials (requirements Feature 4 and Service 8), which 0006 and 0017 left for their own record. It amends 0006; the agent stays one of the ten applications. Amendment (spare multiple choice) approved 2026-11-22 with the plan to finish the AI gaps.

**Context.** A student who wanted to test themselves from the slides had only the chat. Practice has no teacher in the loop, so it cannot rely on teacher review, and it must not touch grades or evidence.

**Decision.**
- `POST /v1/practice` in `question-generation-agent`: a classroom, 1–10 whole documents, optional topic and focus (300 characters), counts for multiple choice and short answer (1–10 each, at most 10 in total), a difficulty, and up to 50 prompts already held. Free response is refused (422): it needs a rubric and a marker.
- Only an enrolled student, with their own token, through the existing route. Calls are billed to the student (`practice.generated`, `practice.reviewed`).
- The floor, rerank rule, checks and critic loop are 0006's and 0015's.
- Review fails closed: it always runs; with `QUESTION_REVIEW` off, `503 AI_UNAVAILABLE`. Only passed drafts are returned; flagged and unjudged ones are dropped. If the reviewer never judged them, `503 REVIEW_UNAVAILABLE`.
- Nothing is saved or graded: no submission, evidence or event. Each question returns its key, explanation, evidence and passages. These AI-written keys for the student's own ungraded use are the one exception to never exposing keys to students; Assessment's keys stay hidden. The page reveals each key only after the student answers. Short answers are checked in the browser after folding case, whitespace and punctuation.
- Spare multiple choice (amendment): the first write asks for N + min(2, ceil(N/2)) multiple choice; a later write asks for what is missing plus one; at most 8 drafts a write. Short answers and teacher requests are unchanged. Served questions are the first that passed, up to the requested count. Prompts are version 4.

**Why.**
- Fail-closed review because no teacher checks practice.
- About one spare per two multiple choice: in the run before spares, the 16 multiple choice asked for drew 10 weak-option rejections, and all 3 missing questions were multiple choice; short answers lost none.
- At most 8 drafts a write stays under the 10 a teacher's write fits in the same `GENERATION_TOKENS`.
- Rejected: a fourth round (breaks the call budget, about 15 s more at the 31.9 s median); serving flagged drafts (breaks fail-closed); easing the reviewer (lets weak questions reach students).
- Cost: the six-case run cost $0.0149 before spares and $0.0157 and $0.0141 after.
- Results 2026-11-22 (`run_generation.py --practice`, prompt version 4, two runs): 18 of 20 then 20 of 20 returned, all passed, against 17 of 20 before. Median latency 31.9 s and 30.6 s, against 40 s; 24 and 20 calls, against 26. Weak-distractor rejections still lead (21 and 17 a run). The off-topic request was refused with no generation call. $0.03 in all. Two runs show the shortfall closing but not a stable rate.

**Consequences and limits.** A set costs up to six calls against the student's $5 allowance. A student may see "None of the practice questions passed the checks". A student could read keys in browser tools, which is accepted for ungraded practice. Not built: saving sets, free-response practice, page ranges, weak-topic targeting, teachers seeing practice.

## 0023 Model profiles and reasoning

**Status:** accepted 2026-11-22. The project owner approved the reliability plan for marking, chat and question generation; this is its shared ai-core part. Amends 0002 and 0003. Amendment (per-call reasoning effort) approved 2026-11-22 with the plan to speed up question generation.

**Context.** Every call ran `gpt-6-luna` with reasoning off. The marking critic and question reviewer used the same model and settings as the writers they check, so errors tended to match. A rejected structured reply ended the work. Timeouts were never retried, one slow rerank dropped results to unranked order, and the ledger did not record how a call ended or how long it took.

**Decision.**
- `POST /v1/inference` takes `model_profile`:

| Profile | Model | Reasoning effort | Used for |
|---|---|---|---|
| `default` | `CHAT_MODEL` (`gpt-6-luna`) | `STRUCTURED_REASONING_EFFORT` (`low`) | writers' replies |
| `check` | `CHECK_MODEL` (`gpt-6-luna`) | `CHECK_REASONING_EFFORT` (`medium`) | critics and reviewers |
| `fast` | `CHAT_MODEL` | `CHAT_REASONING_EFFORT` (`none`) | the chatbot's query rewrite |

- Streamed chat stays at `none` and takes only `default`; any other profile on a stream is 422. The ledger names the model actually called. Callers of `check` since 0024–0026: the marking critic, the question reviewer, the chat support check.
- Amendment: optional `reasoning_effort` (`none`, `low`, `medium`) overrides the profile's effort on the profile's model; a stream accepts only `none`.
- Reasoning headroom: `max_completion_tokens` = `max_tokens` × none 1, minimal 1.25, low 1.5, medium 2.5, high 4 (unknown 4), never below `max_tokens`, capped at 8000. Reservation and timeout charge use the value sent.
- Retries: a timed-out generate or stream opening is retried once if a whole attempt still fits; each timed-out attempt is still charged. The reranker retries once on connection failure, timeout, 429 or 5xx, both attempts within 3 s (each at most 2 s, a retry needs 0.5 s).
- One re-ask: `AgentClients.generate` takes `profile`, `check` and `reask`. A rejected or faulty reply is asked again with the faults ("your previous reply failed these checks: …; return a corrected reply."), same payer and deadline. Still faulty: `AI_OUTPUT_REJECTED` or `CheckFailedError`.
- Migration `007_usage_outcome.sql` adds nullable `outcome` (`ok`, `rejected`, `timeout`, `failed`) and `latency_ms`; additive, `CHECK ... NOT VALID`.

**Why.** Measured 2026-11-22 with real keys, both check models at medium effort:
- Reviewer probe, 3 passes of 13 planted faults and 4 good drafts: both caught 39/39 with no good draft failed; reason named 97.4% on luna ($0.0070) and 100% on `gpt-6.2-sol` ($0.0567).
- Marking eval, 30 cases: both 30/30 agreed, critic doubted none; $0.011 against $0.101.
- `gpt-6-luna` stays the `check` default: same faults caught at about a ninth of the cost. On 0024's harder set sol doubted 4 of 5 real errors and luna 3 of 6 at the same false-doubt rate, which is not conclusive.
- `gpt-6.2-sol` is priced at $2.00 input, $0.10 cached input, $10.00 output per million tokens (OpenAI's standard short-context rate, checked 2026-11-22).
- Latency with low reasoning on writers: marking p50 9.5 s (max 18.6 s) per suggestion with its review; chat p50 3.1 s with the support check. Question generation figures here (p50 41 s with review) were later improved by 0026's amendment (16.8 s).

**Consequences and limits.** Structured replies cost more and take longer than at `none`, with up to 1.5 times the reservation held. ai-core must be updated before any caller sends `check`, `fast` or `reasoning_effort`. A re-ask adds two messages, so a call already at 49 or 50 messages exceeds the 50-message limit. Not built: per-profile spend reports, a vision profile.

## 0024 Marking accuracy and critic profile

**Status:** accepted 2026-11-22. The project owner approved the reliability plan for marking, chat and question generation; this is its marking part. Amends 0004 and 0020. Amendment (harder set and a marking scheme) accepted 2026-11-22 in the next round's plan, which the project owner approved.

**Context.** The reply schema allowed any criterion and any points, so invalid levels were caught only afterwards. Quotes failed on case or curly-quote changes (`UNGROUNDED`). Any failed reply ended the suggestion. Short answers differing only by a full stop still cost a call. The critic shared the marker's settings, and nothing measured agreement with expected levels.

**Decision.**
- `markingSchema(rubric)` limits each criterion to its own id and levels: one item schema for a single criterion, `anyOf` per criterion otherwise. `checkSuggestion` still checks each criterion appears once and every level.
- Quotes match after `fold`: lowercase, curly quotes and Unicode dashes normalized, zero-width characters and soft hyphens dropped, whitespace collapsed. Words must still be the student's own.
- Exact short answers: after `plain` normalization, a trailing full stop or exclamation mark and one pair of wrapping quotes removed, case ignored unless it matters, a match stores a ready full-marks suggestion (`model` `exact_match`, confidence `high`) with no model call. It is still a suggestion; the teacher still marks. Its review is `not_reviewed` with `EXACT_MATCH`.
- The critic sends `model_profile: "check"`; the marker sends `default`.
- One re-ask on `INVALID_OUTPUT`, `UNGROUNDED` or `AI_OUTPUT_REJECTED`, with specific faults, inside the same 90-second `markingCall`, started only with 20 seconds left. Not for `CONTRADICTED` or `EMPTY_OUTPUT`. The critic's re-ask passes back only its own reply.
- `markingTokens` stays 1500 visible tokens; ai-core adds headroom.
- Eval: `scripts/eval/marking_cases.yaml` with `load_marking.py`, `run_marking.py` (`--max-usd`, `--rescore`) and `delete_marking.py`. Metrics: level and case agreement, mean absolute points error, unusable-reply rate, the critic's doubtful, true-doubt and false-doubt rates, its own agreement, expected flags raised.
- Amendment: 23 harder cases (borderline levels, wrong mechanism, contradiction beside a correct sentence, near misses, another language, padding) and a binary-search short answer whose scheme accepts O(log n) but not "logarithmic" or "log time" alone. Planted wrong first markings were tried and dropped: the critic never sees the first marking, so a plant only restates the critic's own accuracy.

**Why.**
- A single union enum of every id and level was rejected: it still lets a criterion take another criterion's level.
- A `CONTRADICTED` reply is not re-asked: the model saw evidence against its own level, and a second try would only teach it to drop that evidence.
- An exact match has no model reply for a second examiner to doubt, like `MARKED_BY_TEACHER`.
- First results (30 cases, 4 questions): level agreement 1.0, points error 0, no unusable replies, 3 exact matches with no call, 2 auto-marked; the critic agreed on all 25 model suggestions with both luna ($0.011) and sol ($0.101). No first marking was wrong, so true doubt was unmeasured.
- Harder set (53 cases, 5 questions, luna marker and critic): level agreement 0.925, case agreement 0.882 (0.875 on 48 model-marked), points error 0.137, no unusable replies, $0.021. The first marking was wrong on 6 cases (5 stricter, 1 more generous). The critic doubted 3 of 6 (true-doubt 0.5) and 1 of 42 good ones (false-doubt 0.024). Every scheme case was marked as expected. The critic's 3 misses were among its own 4 wrong levels: it repeated the marker's error, because both run on luna.
- Sol as critic (same 53 cases): first marking wrong on 5; the critic doubted 4 (true-doubt 0.8) and 1 of 42 (false-doubt 0.024); its own levels wrong on 3. Cost $0.188 against $0.021.
- `CHECK_MODEL` stays `gpt-6-luna`: one more of five errors caught is within run-to-run variation on this sample (the first marking itself varied between 6 and 5 errors), at about nine times the cost billed to the teacher. Switching is one setting and needs the owner's approval.

**Consequences and limits.** A faulty reply can cost two calls; a ready suggestion with its review up to four. Expected levels are drafts, with `teacher_rating: null` until a teacher confirms them. Whether 1500 tokens fit ten criteria is not measured. Single cases vary between runs. Not built: re-asking contradicted replies, broader exact matching, a measured choice of check model.

## 0025 Chat answer support check

**Status:** accepted 2026-11-22. The project owner approved the reliability plan for marking, chat and question generation, and authorized this new paid call in that round. Amends 0002 and builds on 0023's `check` profile. Two amendments (the check after `done`; primary topics and the probe) accepted 2026-11-22 in the next round's plan, which the project owner approved.

**Context.** The chatbot confirmed that `[S#]` tags name prompt passages and that every topic was cited, but not that a passage says what the sentence claims. Content's keyword leg (`websearch_to_tsquery`) required every word, so long questions rarely had keyword hits, and a failed query embedding failed the whole search. Multi-topic answers sometimes cited nothing from one topic's document while the coverage notice stayed silent, because a shared passage counted for both topics.

**Decision.**
- After the answer, one structured call on the `check` profile (`answer_check.v1`), billed to the same user. It gets each cited sentence (at most 24, up to 300 characters each) with the cited passages only, escaped as data. Verdicts: `supported`, `partial`, `unsupported`, with a reason of at most 20 words. One re-ask on a bad sentence id, within 20 s; 1500 visible tokens.
- Skipped for refusals, small talk, errors, interrupted answers and answers with no citations. A failed check logs `answer_check_failed`, saves `{"status": "failed"}` and never fails the turn.
- Order (amended): the turn is saved and `citations` and `done` are sent first; `done` carries `checking: true` when a check will run. The check then runs as a detached task under `asyncio.shield`, so it is saved even if the client leaves. An unsupported notice is appended to the saved `content` and sent as a `notice` event. `SAVE_GRACE` is 25 s inside the 45 s stop grace.
- Notices: any `unsupported` sentence gives "The cited passages may not support: “…”." (up to three, 80 characters each). `partial` gives none. An answer that states something (a declarative sentence of six or more words) with no citations gives "This answer does not cite the materials." with no call (`uncited`).
- Storage: `007_answer_check.sql` adds nullable `answer_check`; reasons are not saved, because they may paraphrase a passage that a later deletion redacts.
- Search: the keyword leg is an OR of `plainto_tsquery` lexemes ranked by `ts_rank_cd`. A failed embedding falls back to keywords alone (`embedding_fallback: true`); the rerank still applies the floors.
- Primary topics (amended): each passage gets one primary topic, the search it ranked best in, then heading match, then topic order. A topic with no primary passage takes a shared one from a topic that has more than one. A topic is covered when a primary passage is cited. The prompt groups documents in `<topic>` blocks (`student_tutor.v5`, `teacher.v4`). `008_passage_topics.sql` stores chunk ids and labels only.
- Probe: `scripts/eval/answer_check_probe.yaml` plants faults (changed number or name, contradiction, claim outside the passages) and controls (paraphrase, condensed restatement, Socratic hint), run by `run_chat.py --probe` through `scripts/eval/answer_probe_inside.py` with the deployed check's code.

**Why.**
- Moving the check after `done` came from the check making `done` wait up to 20 s.
- Rank first, then headings, for primary topics: the best-ranking search is the best evidence of a passage's subject. Keeping all labels and adding a primary one was preferred to dropping a shared passage, which would take evidence away.
- Cost on `gpt-6-luna` ($0.10 input, $0.50 output per million): about 1,000–4,500 input tokens and 200–400 visible output tokens, roughly $0.0005–$0.002 per checked turn, up to twice with a re-ask; $0.01–$0.03 on sol.
- First run (103 cases, 107 turns): 51 answers checked, 113 statements, 83.2% supported, 16.8% partial, 0 unsupported; p50 3.1 s, max 15.9 s. Two cases failed. OR keyword leg: hit@1 0.951 (was 0.924), MRR 0.974 (was 0.950), recall@10 1.0, no leaks.
- After the move (106 cases, 110 turns): time to `done` p50 3.4 s, max 8.1 s; 85.9% supported, 14.1% partial; two other multi-topic cases failed.
- After primary topics: all 106 cases passed (the first clean run); `done` p50 3.2 s, max 7.1 s; 84.5% supported, 13.6% partial, 1.8% unsupported. Probe (21 statements × 3, $0.002): catch rate 1.0, no false alarms, no control partial. Changed numbers were partial 8 of 9 times and names 3 of 6, so a student sees a notice for 69% of planted faults.
- `partial` stays silent: it would mark about one statement in seven of real answers, and the probe cannot yet tell those from real errors. A teacher-rated sample of real partial statements should decide it.

**Consequences and limits.**
- One more call per cited answer against the user's allowance. Since the check moved behind `done`, it no longer delays the answer, and leaving after `done` still saves it.
- The web app shows "Checking the sources…" (`role="status"`) from `done` until the stream closes, then "Sources checked." or "Sources checked: see the note under the answer." The next question can be sent meanwhile. Older clients ignore `notice` and see it on reload.
- The checker is a model's judgment: a wrong `unsupported` shows a notice on a correct answer, and a wrong `supported` shows nothing. The notice says "may not support" for that reason.
- The uncited heuristic can miss an uncited claim in a reply that also says something is not covered, and can note a long hint with no citation.
- The clean chat run is one run after two runs that each failed a different multi-topic case, so it shows the fix working, not a stable pass rate. The probe's rates hold for planted faults of these kinds only.
- The shared `search_content` helper does not yet read `embedding_fallback`, so the agents cannot log it; Content's own search log does.

## 0026 Independent question review

**Status:** accepted 2026-11-22. The project owner approved the reliability plan for marking, chat and question generation; this is its question generation part. Amends 0006, 0015 and 0022, and uses 0023's profiles and re-ask. Amendment (faster generation and better distractors) approved 2026-11-22 with the plan to speed up question generation.

**Context.** The reviewer shared the writer's model and settings. Nothing made it say whether the passages support the key or whether a second option is also right. Guidance had no review. An unreadable reply ended the request. The reviewer probe (`scripts/eval/review_probe.yaml`) missed 2 of 13 planted faults: `groups-shown` ("Using the three groups shown…", a list only the materials show) and `transaction-guarantee` (two right options, since the passage gives both atomicity and isolation).

**Decision.**
- Question, practice and guidance reviews send `model_profile: "check"`; writers stay on `default`.
- Each review adds `evidence_supports_key` and, for multiple choice, `other_option_also_correct`, worked out after `own_answer` and before reading the key. Code applies them even on `pass`: a second right option fails as `AMBIGUOUS`, an unsupported key as `NOT_ANSWERABLE`, a missing value fails closed. Prompts version 2.
- `checks.points_at_materials` drops pointer wording ("the three groups shown,", "the steps listed above") as `NEEDS_MATERIALS` with no model call, and leaves ordinary wording ("the companies listed on the exchange", "which of the options listed") alone. The `AMBIGUOUS` rule now applies "even when the key is the better answer".
- One re-ask for a write ai-core rejects or that has no `questions` list. `MAX_CALLS = 6` covers re-asks: a re-ask costs a later round, never a seventh call. With the review off, at most 2 calls. Deadline and the 1.5-times rule are unchanged.
- `POST /v1/marking-guidance` with review on: write, check, re-ask once if needed; review on the check profile (`marking.guidance.reviewed`) without the evidence; a failed review uses the unused re-ask; still failing, `422 GUIDANCE_REJECTED`. At most 4 calls. If the first review cannot run, guidance is returned, because the teacher reviews it before use.
- Amendment: a teacher's draft that fails only on `WEAK_DISTRACTORS`, with both key checks holding, counts toward its type and is served flagged without a rewrite. Practice is unchanged. `QUESTION_REVIEW_EFFORT` (`none`, `low`, `medium`) sets the reviewers' effort; unset keeps the profile's. The writer gets wrong-option rules (plausible half-knowledge mistakes, matching length and grammar, no "all/none of the above", none restating the key), and the reviewer names `weak_options` to replace. Prompts version 3.

**Why.**
- `groups-shown` is a wording fault, so a deterministic check catches it for free; `transaction-guarantee` is about meaning, so only the reviewer's option check can.
- Results 2026-11-22: the probe on the check profile caught 13 of 13 with no good draft failed (was 11 of 13), over three passes, on luna (reason named 97.4%, $0.0070) and sol (100%, $0.0567). Generation A/B (13 runs, $0.021): with review, 22 of 22 returned (20 passed, 2 flagged) after 19 drafts were sent back (13 weak options, 3 second right answers, 3 invalid keys), where on 2026-10-27 none were; without review, 21 of 22. Review cost 32 calls against 6 and p50 41 s against 8.8 s. Practice: 17 of 20 returned, all passed.
- After the amendment (prompt version 3): the probe at low effort caught 97.4% with 8.3% of good drafts wrongly failed, against 100% and 0% at medium, so reviewers stay at medium. Teacher generation with review: 22 of 22 returned (16 passed, 6 flagged and served without a rewrite), 14 calls against 32, p50 16.8 s against 41 s, $0.009. Practice: 17 of 20, weak-distractor rejections down from 14 to 10, p50 40 s. The wrong-option rules helped the writer only a little.
- Teacher weak-option drafts are not rewritten because the teacher reviews them anyway; practice keeps fail-closed because no teacher does (0022).

**Consequences and limits.** Reviews use reasoning tokens and cost more; a slow round leaves less room for a retry inside 240 s. Guidance costs up to four calls. The pointer check can drop an ordinary question ending on "shown" ("Name one of the four elements shown."), which the writer can reword. The agent counts its own requests by overriding `_generate_once`. Not built: a re-ask for an unreadable review, and extra searching when a draft is sent back. Practice sets can still come back short.
