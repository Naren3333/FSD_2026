# Learning Ledger

A working local foundation for a personalized learning platform: five Go services, five Python services, Angular, Kong OSS, Kafka, PostgreSQL and S3-compatible storage. AI inference is deliberately unimplemented. No provider keys are needed.

The initial repository was empty. The original brief is preserved in [docs/requirements.md](docs/requirements.md); architectural decisions are in [ADR 0001](docs/decisions/0001-foundation.md). See [the engineering handover](docs/development/handover.md) for verification and incomplete requirements.

## Applications and ownership

| Service | Language | Responsibility and local persistence |
|---|---|---|
| identity | Go | Organizations, verified user profiles, classrooms, enrollments; `identity` database |
| curriculum | Go | Subject/course/unit/topic/skill/objective CRUD and skill prerequisites; `curriculum` database |
| content | Go | Authorized original uploads, metadata, revision number, real text chunks; `content` database and restricted S3 bucket |
| assessment | Go | Manually authored immutable assessments, answer keys, submissions; `assessment` database |
| grading | Go | Exact objective grading, drafts, teacher overrides/finalization and audit; `grading` database |
| analytics | Python | Finalized per-question evidence and derived skill accuracy; `analytics` database |
| ai-core | Python | Validated inference/embedding contract; stateless, returns 501 |
| question-generation-agent | Python | Validated practice-generation contract; stateless, returns 501 |
| student-performance-agent | Python | Validated evidence-interpretation contract; stateless, returns 501 |
| question-chatbot-agent | Python | Validated authorized RAG/chat contract; stateless, returns 501 |

Every application has its own dependencies, Dockerfile and tests. Build context is the repository root so a small transport/operational package can be vendored into each image. No shared domain models or coordinated deployment requirement. Kong, Keycloak, Kafka, PostgreSQL, MinIO and web are infrastructure/client applications, not additional business services.

## Start locally

Prerequisites: Docker with Compose v2, Python 3.12+ for setup. Go 1.25+, Node 22.12+ and Python dependencies are needed only for host-side quality checks. The tested host uses Go 1.27.1, Python 3.13.5 and Node 22.17.0.

```sh
python scripts/setup.py
docker compose up -d --build
docker compose ps
```

Open **http://localhost:4200** and sign in. Local usernames are `teacher`, `student` and `other-teacher`; their generated passwords are in `.env` under `TEACHER_PASSWORD`, `STUDENT_PASSWORD`, and `OTHER_TEACHER_PASSWORD`. Credentials are never committed. Keycloak is at http://localhost:8081, gateway at http://localhost:8000. Only these three ports bind to loopback; databases, broker, bucket, agents and Kong Admin API have no host listeners. Kong Admin API is disabled entirely.

Initial container pulls/builds can take several minutes. `gateway-config` waits for Keycloak, `kafka-init` creates topics, and `storage-init` creates a restricted content identity. These one-shot containers exit successfully; they are not application services. PostgreSQL migrations execute transactionally on application startup. MinIO images use the official Quay registry because Docker Hub images were unavailable at implementation time.

The setup script refuses to overwrite `.env`. Keep `.env`, `.local/postgres-init.sql`, `.local/learning-realm.json` and the Docker volumes together. Re-running setup with new secrets against existing volumes does not rotate database or identity credentials. Never delete volumes merely to resolve a startup error.

### Try the workflow

1. Sign in as the student once and copy their ID from the header.
2. In another browser profile, sign in as teacher, create a classroom, select it, and enroll that ID.
3. Add a curriculum skill. Upload a UTF-8 text document if desired.
4. Create a numerical or multiple-choice assessment. The UI authors one question; the API supports 1–50.
5. As student, refresh classrooms, choose the classroom, open the assessment and submit answers.
6. As teacher, refresh Grading review. Inspect the draft, provide review notes, and finalize it.
7. As student or classroom teacher, open Learning performance and request the student's evidence. Kafka delivery is asynchronous; refresh if processing has not completed.

AI guidance is visibly unavailable. Uploaded text is **extracted, not indexed**. PDFs receive `extraction_unsupported`, with their originals retained. No vectors, chatbot answers, fake performance, or generated questions are returned.

## Development and checks

Create a virtual environment and install test tooling:

```sh
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# POSIX: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cd apps/web
npm ci
npx playwright install chromium
cd ../..
python scripts/check.py
python scripts/integration.py
cd apps/web
npm run test:e2e
```

`check.py` runs Go formatting checks, vet, tests, builds; Python Ruff formatting/lint, mypy and pytest; Angular lint, strict types, unit tests and production build. It stops at the first failure. Integration tests require real running infrastructure and create uniquely named test records without deleting user data. Browser E2E uses Keycloak sign-in and gateway APIs; it never bypasses Kong. Playwright traces can contain local test sessions; generated reports are ignored by Git.

Independent Go checks: from any `services/<go-service>` directory run `go test ./...`, `go vet ./...`, `go build ./...`. Independent Python checks: install that service's `requirements.txt` and the local `packages/python-platform` package, then run `pytest` and `mypy main.py` from its directory. For source-based development set `PYTHONPATH` and `MYPYPATH` to the absolute `packages/python-platform` directory. Start with `uvicorn main:app --port 8080`. Each app consumes its own configuration below.

Convenience Make targets mirror these commands. `make down` preserves volumes. Windows does not require Make; use the commands directly. Run `python scripts/export_contracts.py` to regenerate the ten OpenAPI files and three JSON Schema event contracts.

## Configuration

| Variable | Applications | Purpose |
|---|---|---|
| `DATABASE_URL` | Six persistent services | Exclusive service database URL; for Neon use `sslmode=verify-full` and the service's own role/database |
| `OIDC_ISSUER` | All ten | Exact external realm issuer |
| `OIDC_JWKS_URL` | All ten | Reachable JWKS endpoint; may use private networking |
| `OIDC_AUDIENCE` | All ten | Expected audience, locally `learning` |
| `IDENTITY_URL` | Business services/analytics | Classroom authorization API, forwarded verified bearer token |
| `CURRICULUM_URL` | assessment | Skill-reference validation API |
| `KAFKA_BROKERS` | content/assessment/grading/analytics | Comma-separated broker addresses |
| `KAFKA_GROUP` | analytics | Consumer group; default `analytics-v1` |
| `S3_ENDPOINT`, `S3_BUCKET` | content | S3-compatible endpoint without scheme, document bucket |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | content | Restricted content-only S3 identity |
| `S3_SECURE` | content | `true` for TLS, explicitly `false` in local Compose |
| `PORT` | Go services | HTTP port, default 8080 |

Root `.env.example` lists local provisioning secrets. Compose maps them to each service without exposing other services' database passwords. Production deployments must provide environment-specific OIDC, storage and Neon settings, TLS, broker ACLs, external secret management, proper Keycloak persistence, and migration/runtime role separation. The Angular API and OIDC origins are currently local constants in `src/api.ts`; configure a production build before deployment.

## Implemented Kafka flow

`content.uploaded.v1` records accepted metadata. `assessment.submitted.v1` carries the immutable question/answer snapshot to grading. `grading.finalized.v1` carries only teacher-approved evidence to analytics. Grading and analytics consumer groups are independent. Both consume with manual commit after database transaction success and deduplicate event IDs; they also deduplicate submission/evaluation business IDs.

Outbox publishing is at least once. Rows are locked with `FOR UPDATE SKIP LOCKED`, published with all replicas acknowledging, then marked sent. An acknowledgment/DB-commit gap can replay an event safely. Publishing retries cap at 12 and retain failed rows. Consumers retry transient failures five times, then stop without acknowledging. Invalid contracts go to `.dlq` and commit only after durable dead-letter delivery. See [operations](docs/development/operations.md) for recovery and replay limits.

## Scope and limitations

This is a local foundation, **not production ready**. Lists are capped (100, curriculum 200) without pagination. The UI has a one-question editor and no override editor, although the API accepts teacher overrides. Content supports initial revision 1 only; revisions, lesson plans, conversations, PDF extraction and vector retrieval are future work. Assessment publication is immediate/immutable and one submission per student is enforced; scheduling, separate reusable question-bank APIs and resits are future work. Analytics currently reports skill accuracy, not topic/trend dashboards or validated mastery. AI applications require an `ai-client` role and have no public gateway routes. Future implementation must authorize every requested content/student reference before retrieving anything.

Local Kafka uses a private Docker network with plaintext and no ACLs; this is not the production broker security model. Keycloak `start-dev` and the local `learning-e2e` password-grant client exist only for reproducible tests; do not deploy that realm in production. The MinIO community project is archived; choose a maintained production S3 provider. See the handover for all remaining work and exact test results.
