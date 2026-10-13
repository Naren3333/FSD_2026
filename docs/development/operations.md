# Operations and recovery

## Upgrading a local setup made before ADR 0002

`scripts/setup.py` now also generates the AI service-account secrets (`CONTENT_INDEXER_CLIENT_SECRET`, `QUESTION_CHATBOT_AGENT_CLIENT_SECRET`), adds their Keycloak clients to the realm, and creates the `vector` extension in Content's database. It never overwrites an existing `.env`, and Keycloak and Postgres only import on an empty volume.

If you have no local data you want to keep, back up `.env`, then run `docker compose down -v`, delete `.env` and `.local/`, rerun `python scripts/setup.py`, and run `docker compose up -d --build`. **This deletes every local database, upload and Keycloak user.**

Document storage moved from MinIO to SeaweedFS (volume `seaweedfs-data`). Documents in an old `minio-data` volume are not migrated; re-upload them, or copy them out with any S3 client before removing that volume.

ai-core reads `OPENAI_API_KEY` and `VOYAGE_API_KEY` from `.env`. A new setup writes them empty; an older `.env` needs the two lines added by hand. Without them ai-core starts and answers `503 AI_PROVIDER_NOT_CONFIGURED`, and the integration test accepts that.

The chatbot (stage 4) has its own database: run `python scripts/upgrade_local.py` once with the stack up. It adds `CHATBOT_DB_PASSWORD` to `.env` and creates the `chatbot` role and database, changing nothing that exists. Then `docker compose up -d --build` and `docker compose restart kong` (new `/api/chatbot` route).

Content indexing (stage 3) needs no reset: Content's migration `002_indexing.sql` applies on start and keeps existing documents. Run `docker compose up -d --build`, then `docker compose restart kong` so Kong loads the larger Content body limit; `kafka-init` creates the new `content.indexed.v1` topic.

The Postgres image also changed from Alpine to Debian (pgvector), so text-index collations differ. Recreating the volume is the clean path. Keeping data means reindexing every database (`REINDEX DATABASE <name>;` as each owner) after the switch, plus adding the clients and extension by hand.

## Health and logs

Each application exposes `/health/live` and `/health/ready` on its private port 8080. Persistent apps check PostgreSQL; Content also checks the document bucket; Grading marks readiness unavailable after its consumer stops; Analytics checks its database and consumer state. The two AI placeholders (question generation and performance interpretation) need only valid authentication configuration; the chatbot also checks its database. ai-core's readiness reports whether the OpenAI and Voyage keys are set, without calling either (a paid probe on every check would cost money and latency); a provider outage shows per request as `502 AI_PROVIDER_FAILED`.

ai-core keeps one disk cache, the `ai-core-cache` volume: vision-model answers keyed by page content, model and prompt version, so re-indexing an unchanged document makes no vision calls. It holds no business records and can be deleted at any time; the next ingest repopulates it at the cost of new model calls. A readiness probe is a dependency snapshot, not proof of fresh analytical results.

Go uses JSON slog and Python emits JSON application telemetry. Request IDs and correlation IDs are returned in headers; correlation IDs flow across authorization HTTP requests and event envelopes. Tokens, bodies and student answers are not logged. Third-party server/broker startup diagnostics may use their own format. Extend these boundaries with OpenTelemetry HTTP/Kafka propagation and trace exporters in a later milestone; no tracing collector is installed.


## Content indexing

PDF uploads are indexed by Content's worker through ai-core. A document's `status` shows the state: `indexing`, `indexed`, or `failed` with `failure_reason` (`NO_TEXT_LAYER`, `ENCRYPTED`, `CORRUPT` from the PDF itself; `AI_UNAVAILABLE` after six failed attempts to reach ai-core or its providers; `INTERNAL` for a result Content could not store). Pending work is in Content's `index_jobs` table: `attempts`, `next_attempt` and `last_error` explain a waiting job, for example `ai-core 503 AI_PROVIDER_NOT_CONFIGURED` until provider keys are set. After fixing the cause, a teacher (or `POST /v1/documents/{id}/reindex`) queues the document again; PDFs uploaded before indexing existed (`extraction_unsupported`) are indexed the same way. Each ingest costs model calls, so do not loop reindexes.

`python scripts/content_db_tests.py` runs Content's database-backed Go tests (worker, duplicate and stale jobs, retries, search) in a Go container on the Compose network. It creates a separate `content_test` database once and works in throwaway schemas there; Content's own data is not touched. They are skipped by `check.py`, which has no database.
## Chatbot

A chat turn needs Content, ai-core with an OpenAI key, and (for reranking) a Voyage key. Without a key the turn ends with an `AI_UNAVAILABLE` error event and the message is saved with outcome `error`; without Voyage, search falls back to RRF order, which the relevance floor cannot check, so the turn ends with a `SEARCH_UNAVAILABLE` error instead of an answer. `RELEVANCE_FLOOR` (default 0.61, fsd_test's tuned value for Voyage rerank scores) and `ANSWER_MAX_TOKENS` (800) are optional environment settings. Each saved answer records its rewritten query, the chunk ids it was given, its citations, prompt version, model, tokens and latency, which is what to look at when an answer is wrong. The Postgres store tests run with `CHATBOT_TEST_DATABASE_URL` pointing at a scratch database.

## Retrieval eval (golden set)

`scripts/eval/` scores Content's search through Kong as real local users, with fsd_test's golden queries (`golden.yaml`, 267 queries) and the same scoring, so results compare with fsd_test's runs. The test PDFs are course material and are not in this repository: they are read from `EVAL_PDF_DIR` (default `../fsd_test/data/test_pdfs`), as listed in `corpus.yaml`.

1. `python scripts/eval/load_corpus.py` creates the classrooms "Golden eval C1" and "Golden eval C2" as `teacher`, enrolls `student` in C1, creates a second local test student `eval-student2` for C2 (its generated password and the classroom and document ids are kept in `.local/eval-corpus.json`), uploads the PDFs and waits for indexing. It is safe to re-run.
2. `python scripts/eval/run_golden.py --rerank --holdout [--label NAME] [--compare .local/eval-runs/EARLIER.json]` prints hit@1/3/5, MRR, recall, leaks and the refusal-floor table, including where the chatbot's `RELEVANCE_FLOOR` 0.61 falls. It exits 1 on a leak. Runs are saved in `.local/eval-runs/`.

Costs: indexing pays for embeddings and for vision calls on pages ai-core has not cached (copying fsd_test's `data/table_vision/` into ai-core's cache volume under `vision/`, owned by uid 10001, makes the vision pass free); each eval query pays one embedding and one Voyage rerank per classroom searched. Teacher queries search both classrooms and keep the best-scoring results, because search is per classroom. Queries with a `topic_ids` filter are skipped: Content has no topics.

Baseline (2026-10-13, `rerank-3`, all 266 runnable queries): hit@1 0.959, hit@3 0.991, MRR 0.975, recall@5 0.962, 0 leaks; held-back split MRR 0.967. The best refusal floor is 0.613, which refuses 93% of unanswerable queries and keeps 96% of correct answers. These match fsd_test's run on the same queries.

`python scripts/eval/run_chat.py [--only GROUP] [--label NAME]` runs real chat turns through Kong in the same classrooms: 96 cases in 100 turns. Groups: `refusal` (golden's unanswerable queries), `leaks` (students asking for rubric, marking-scheme or other-class content), `grounded` (golden questions that must be answered with a citation on an expected page), `followup`, `injection` (instructions planted in documents or typed by the user), `rubric` (teachers answered from teacher-only documents) and `socratic` (transcripts to read). It reads what the API doesn't return, such as the chunks a turn retrieved, from the chatbot's and Content's local databases. It exits 1 on an error, a leak, a citation that wasn't retrieved, an obeyed injection, or a grounded answer that cites none of the expected pages. Every turn costs model calls: the first run used about 91,000 tokens.

First chat run (2026-10-13): 1 failure in 100 turns, 0 leaks, 0 obeyed injections. The failure is `waterfall-weakness`, refused because its correct passage scores 0.48, under the 0.61 floor. Three unanswerable questions were answered (Kanban, JUnit assertions, a quiz question 4 that doesn't exist); each answer says the materials don't cover it.

## Outbox retry and broker recovery

Inspect only the affected service's `outbox` using its own database identity. Rows with `published_at IS NULL AND attempts >= 12` require operator action after repairing the broker. To retry a reviewed event, use a parameterized update on that specific event ID: set `attempts=0, next_attempt=now()` while retaining its original `id`, payload and creation time. Never invent a replacement grade or delete outbox history to clear an alert.

Consumers retry transient processing errors five times. On exhaustion they stop without committing the Kafka offset; readiness must be investigated and the service restarted after the dependency is repaired. Invalid messages go to `assessment.submitted.v1.dlq` or `grading.finalized.v1.dlq`. Dead-letter content is confidential. Correct the source/contract issue, then replay only reviewed records. Preserve original event IDs. Analytics also protects against a new ID for an already processed evaluation.

Kong's OSS JWT plugin uses Keycloak's provisioned public key. Following a planned signing-key rotation, rerun `docker compose run --rm gateway-config`, then restart Kong. Backends use JWKS refresh. For production, automate overlapping-key provisioning before retiring a key; the local single-key setup is not a complete zero-downtime rotation system.

## Rebuilding analytics

Kafka's local retention is 30 days. Within retention, rebuild into a **new empty Analytics database owned by the Analytics role**, set `KAFKA_GROUP` to a new group and consume from earliest. Validate counts against finalized grading records before switching traffic. Never reset offsets while retaining the same inbox if the goal is rebuilding missing evidence. Historical grades beyond retention require a durable event archive or an authenticated Grading reconciliation/export API; neither is implemented. Do not claim indefinite reconstruction from the current broker alone.

## Production boundary

Local Compose is a development deployment only. Before production: deploy HTTPS OIDC and application origins, managed Keycloak persistence/backups, Kafka TLS/SASL with topic-scoped producer/consumer ACLs, per-service broker credentials, certificate-verified Neon URLs, separate migration/runtime roles, managed secrets, retention/backup/restore tests, and a maintained S3 provider. Remove the local E2E password-grant client and test accounts. Pin image digests, update supported patch versions, and run vulnerability/license scanning.

The private agents require a standard OIDC client-credentials identity carrying `ai-client` and the expected audience/organization. No such privileged application role is granted to ordinary teacher/student users. Future agent workflow authorization must use delegated user authorization or a narrowly scoped token exchange, never arbitrary input identity fields.

## Limits and consistency

HTTP requests are bounded; database pools are capped; uploads are limited to 5 MiB; assessments allow 50 questions; synchronous HTTP authorization calls have five-second timeouts. Lists currently have fixed caps and need cursor pagination. Analytics reads all scoped evidence to summarize it; high-volume deployments need SQL aggregation/materialized read-model metrics. Kafka operations do not hold a chain of service calls. Identity unavailability fails resource authorization closed.

S3 and PostgreSQL cannot share a transaction. Content uploads the object, then commits metadata/outbox, with best-effort compensation on known failure. A process crash can leave an orphan object. Reconciliation and ambiguous-commit handling are remaining risks; production needs a staged upload/finalization saga and a reviewed orphan cleanup job. No automated destructive cleanup is implemented.
