# Engineering handover

## Delivered architecture

The repository began empty. It now contains exactly ten independent application services, their Dockerfiles and tests, an Angular client, and reproducible local Kong OSS / Keycloak / Kafka / PostgreSQL / MinIO infrastructure. No existing application or data was replaced. Service responsibilities, data ownership, diagrams and authentication/event boundaries are documented in `docs/architecture/system.md`.

Go implements Identity/Classroom, Curriculum, Content, Assessment and Grading. These are transactional, explicitly authorized operations with bounded request handling and SQL transactions. Python/FastAPI implements deterministic Analytics and four future AI applications, with Pydantic contracts suitable for later analytical/model tooling. Angular 21 with strict TypeScript implements the browser. Shell/SQL/configuration are operational artifacts; no third backend language or extra business service was introduced.

## Repository layout

```text
apps/web/                          Angular features, API client, shared form/status controls, E2E
services/{identity,curriculum,content,assessment,grading}/
                                   Independent Go modules, SQL migrations, tests, Dockerfiles
services/{analytics,ai-core,question-generation-agent,
          student-performance-agent,question-chatbot-agent}/
                                   Independent Python apps, requirements, tests, Dockerfiles
packages/platform/                 Go HTTP/auth/db/event operational helpers
packages/python-platform/          Python auth/error/logging helpers
contracts/{http,events}/            Ten OpenAPI documents, three event schemas
infra/                             Kong config generator, restricted storage policy
scripts/                           Setup, checks, readiness, contracts and real integration
docs/                              Original brief, inspection plan, ADR, architecture, operations
.github/workflows/quality.yml       Independent service checks and live integration/E2E workflow
docker-compose.yml                 Complete local deployment
DESIGN.md / UX-CONTRACT.md          UI design and behavioral ownership
```

## Working non-AI behavior

- OIDC authorization code + PKCE login. Services verify RS256, issuer, audience, expiration and organization. Resource policy comes from Identity. Unknown identity headers confer no privileges.
- User-profile registration, teacher-owned classroom creation/listing, same-organization student enrollment, classroom/student access checks.
- Curriculum node create/read/rename/archive, stable IDs, parent validation, and prerequisite insertion with serialized cycle detection.
- Authorized uploads into a restricted S3 bucket, real UTF-8 extraction/chunk provenance, SHA-256, original-file retrieval and metadata. PDFs are retained with an honest unsupported-extraction status.
- Immutable manually authored multiple-choice/numerical assessments, enrolled-student submission, answer-key-safe student projection, and one submission per assessment/student.
- Kafka-triggered deterministic draft grading with exact rational decimal arithmetic and inclusive tolerance. Teacher review is mandatory; API overrides are bounded, and finalization writes an audit trail and event atomically.
- Finalized per-question evidence consumed into Analytics. Skill accuracy, attempted/correct counts and explicit insufficient-evidence status come from actual results, with no mastery-probability claim.
- Angular classroom, curriculum, upload, assessment authoring/submission, grading review and performance interfaces calling Kong. No canned chatbot or invented dashboard data.

## AI contracts and future interfaces

The private AI Service exposes `/v1/inference`; Question Generation exposes `/v1/question_generation`; Student Performance exposes `/v1/performance_interpretation`; Question Chatbot exposes `/v1/grounded_question_answering`. Each checks authentication and the `ai-client` role, validates its request, and returns HTTP 501 with `AI_CAPABILITY_NOT_IMPLEMENTED` and a capability name. Normal users do not receive the internal role. No agent route is published by Kong and none has a database.

OpenAPI defines request and future response schemas (questions/keys/explanations/provenance, evidence-backed observations/interventions, answers/citations/insufficient evidence, provider-independent generation/embedding output). Content declares future embedding-version and authorized-retrieval ports. These are **interfaces only**. Tests replace verified claims with clearly scoped test doubles for placeholder behavior; runtime code never registers those doubles. Separate auth tests exercise real RSA signature, expiry, issuer and audience verification.

## Events and persistence

Implemented topics: `content.uploaded.v1`, `assessment.submitted.v1`, `grading.finalized.v1`; dead-letter topics: `assessment.submitted.v1.dlq`, `grading.finalized.v1.dlq`. Each envelope has UUID event/correlation/resource IDs, type, schema version, UTC timestamp and typed payload. Content never emits an indexed event; Analytics does not yet publish a performance-updated event.

Six service-owned databases use independent non-superuser credentials and deny public connection privileges. Four AI applications are stateless. Producers write outboxes in the business transaction and publish at least once. Consumers commit Kafka offsets only after inbox/business transaction success or confirmed dead-letter delivery. Event-ID plus natural business-key deduplication covers replay and new IDs for the same finalized evaluation. Outbox attempts and consumer retries are bounded; operator recovery is documented.

## Verification evidence

The initial verification completed:

| Command | Actual result |
|---|---|
| `python scripts/check.py` | Passed: six Go modules' formatting/vet/tests/builds; all five Python services' Ruff/mypy/pytest; Angular ESLint/types/unit tests/production build |
| `python -m pytest -q packages/python-platform` | Passed, real JWT verification test |
| `docker compose build` | All ten service images and Angular image built |
| `docker compose up -d` | All ten services and web started with real local dependencies |
| `python scripts/integration.py` | Passed initial gateway/DB/storage/Kafka workflow, privacy failures, teacher finalization, consumer restart, duplicate delivery/business-key idempotency and database isolation |
| Premium UI strict static audit | Passed, zero findings; this does not substitute for browser/a11y checks |

Final verification on 2026-09-28:

- `docker compose up -d --build` completed; `python scripts/wait_ready.py` reported all ten applications ready.
- `python scripts/check.py` passed the complete Go/Python/Angular quality gate. The final analytics response change also passed Ruff, mypy and its four unit tests. Its typed contract rejects accuracy inconsistent with actual counts.
- `python scripts/integration.py` passed against real Kong, Keycloak, PostgreSQL, S3 and Kafka, including committed HTTP/event schema validation, original-file retrieval, authorization failures, consumer restart, duplicate-event/business-key idempotency and database isolation.
- The final web container built successfully. Frontend lint, strict type checking and both unit tests passed after the last UI changes.
- `npm run test:e2e` passed **3 tests in 49.7 seconds**: real teacher/student assessment → review → finalized performance through Kong; desktop and 390px mobile layout without page overflow; keyboard entry, honest AI-unavailable state, field validation, draft protection/clearing, pending feedback, and network failure/recovery. Network-failure injection is an explicitly controlled browser test; the successful workflow uses real services.
- The premium UI strict audit passed with zero findings. Desktop/mobile screenshots are generated under `apps/web/test-results/` and ignored by Git.

Non-fatal tool warnings included a third-party AnyIO deprecation and pytest cache-write permissions; test assertions and quality commands still completed successfully. No hosted CI run, live Neon test or production deployment has been performed. Earlier failures were fixed rather than suppressed: unavailable MinIO Docker Hub images, Angular/Vitest peer-version mismatch, test-runner suite discovery, source-based Python type resolution, sandbox compiler path access, duplicate DOM IDs, duplicate grading status announcements, and mobile grid overflow. Unsaved-state checks now use actual form state rather than rendered CSS classes, and closing the grading dialog clears its draft consistently.

## Incomplete requirements and risks

The full requested definition of done is **not claimed**. The coherent core workflow is implemented, but these requirements remain incomplete or narrower than the full brief:

- Separate reusable question-bank CRUD, assessment drafts/publication scheduling, assignments with due dates, resits, and richer rubric/draft-feedback workflows. Assessments currently publish immediately and support one submission.
- Document revision workflows beyond revision 1, lesson-plan/conversation persistence, PDF extraction, vector storage/retrieval and all actual AI capabilities.
- Topic-level analytics, assessment trends, richer teacher summaries and durable archival/reconciliation beyond Kafka's configured 30-day retention.
- Complete CRUD/pagination in the UI. Lists are capped; the assessment editor authors one question, and teacher override editing is API-only.
- Production Kafka TLS/SASL/ACLs, deployment network policies, per-service machine identities, managed OIDC persistence, automated signing-key rollover, separate migration/runtime roles, backups/restore testing, secret management and dependency/image vulnerability audits.
- Neon configuration is supported by service URLs but was not tested against a live Neon project; pgvector is a documented future migration, not installed or populated.
- S3/SQL atomicity requires a staged-upload saga and orphan reconciliation. Ambiguous commits retain originals to avoid deleting a possibly committed document. No destructive cleanup is scheduled.
- Static checks and focused browser tests are not a comprehensive WCAG/assistive-technology, multi-browser, offline, load, chaos or security assessment.
- Initial migrations are real and non-destructive; a general ordered migration upgrade runner is still needed for subsequent schema versions.

Recommended next milestone: harden this verified non-AI workflow first—production authentication/broker/database security, pagination, lifecycle/versioning, reconciliation/backups and broader failure-path tests—then implement authorized Content retrieval and genuine inference behind the existing four private AI contracts. Preserve teacher approval and evidence provenance throughout.
