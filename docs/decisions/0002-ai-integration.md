# ADR 0002: Integrating the fsd_test RAG and chat services

Status: accepted 2026-10-13 by the project owner. Supersedes the "AI is deferred" paragraph of ADR 0001 for the capabilities named here.

## Context

`fsd_test` is a separate, working test harness for RAG (retrieval-augmented generation) and chat over teachers' PDFs. It has a `knowledge` service (PDF ingest with vision, table, figure and OCR passes; hybrid pgvector and full-text search; Voyage reranking) and an `assistant` service (threads, query rewrite, cited streaming answers). It identified users through a client-supplied `X-Principal` header and kept its data in two Neon projects.

The owner decided to:
- fold it into FSD_2026 rather than add services;
- keep the ten-service layout of ADR 0001;
- use local Docker Postgres rather than Neon for development.

## Decision

- **Content** stays the only owner of documents.
  - It also owns their chunks, embeddings and search index, in its own database, using pgvector.
  - Search is an authorized Content API scoped by classroom, with the same Identity membership check as every other Content read.
  - No other service reads its tables.
- **ai-core** implements model work: ingest of PDFs into chunks with embeddings, query embeddings, reranking and generation.
  - It stays stateless and accepts only callers holding the `ai-client` role.
  - Provider keys (OpenAI, Voyage) exist only in its environment.
- **question-chatbot-agent** implements grounded question answering.
  - It owns a new `chatbot` database for threads and messages. This is a real persistence need, not symmetry.
  - It is published through Kong for students and teachers.
  - It searches Content with the **calling user's own token**, so it can never see more than that user.
- **Service accounts:** Content's indexer and the chatbot call ai-core with Keycloak client-credentials tokens (`content-indexer`, `question-chatbot-agent`) that carry `ai-client` and the audience.
  - Locally, their `org_id` is a fixed claim. Multi-organization deployments need delegated identity (token exchange) before this is production-ready.
- **Identity:** the `X-Principal` header is removed. Identity always comes from verified JWTs.
- **Unchanged:** question-generation-agent and student-performance-agent keep returning 501 until their own implementation is authorized. (Question generation was authorized in ADR 0006.)

## Consequences

- **Postgres image:** the local image becomes `pgvector/pgvector:0.8.1-pg17` (Debian based).
  - pgvector is not a trusted extension, so `scripts/setup.py` has the superuser create it in Content's database.
  - An existing volume from the earlier `postgres:17.6-alpine` image should be recreated, because musl and glibc collations differ for text indexes. Alternatively, reindex text indexes after the switch.
- **Object storage:** MinIO's images are no longer publicly downloadable (quay.io and Docker Hub both refuse anonymous pulls), so local S3 storage is now SeaweedFS (`chrislusf/seaweedfs:4.48`). Content still uses the S3 API through minio-go. The restricted `content-service` identity can only read, write and list the `documents` bucket, which replaces `infra/storage-policy.json`. This is not an AI change, but it blocked every local start.
- **Line endings:** `.gitattributes` keeps LF line endings on Windows, so `gofmt` in `scripts/check.py` passes there.
- **Migrations:** Go services now apply numbered migrations (`migrations/NNN_name.sql`) once each, recorded in `schema_migrations`, under the existing advisory lock. Content's chunk and index tables arrive as `002_*.sql`.
- **Chatbot database:** the chatbot gets the seventh service-owned database and credential, with the same isolation as the other six.
- **Delivery:** the work lands in stages: foundations, ai-core, Content indexing and search, chatbot, Angular, then documentation. See `docs/development/handover.md` for what each stage verified.
