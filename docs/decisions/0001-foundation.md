# ADR 0001: Independent services and explicit ownership

Status: accepted for the foundation.

Exactly ten application services implement the requested deployment boundaries. Go owns transactional classroom, curriculum, content, assessment, and grading operations; its standard HTTP stack and static typing suit these bounded workflows. Python/FastAPI owns deterministic analytics and four future AI applications, providing typed contracts without model dependencies. Angular 21 LTS with strict TypeScript owns the browser.

Kong OSS performs public routing, JWT signature/expiry verification, rate limiting, request limits, CORS, and correlation IDs. Keycloak provides OIDC; no custom passwords or signing protocols are implemented. Services independently verify issuer, audience, expiry, role, and resource access. The OSS JWT plugin uses a provisioned public key; key rotation requires regenerating gateway configuration. Enterprise OIDC plugins are not assumed.

PostgreSQL uses a separate database and non-superuser login for each of six persistent services. No cross-database queries or shared domain models. Neon is the production target via per-service DATABASE_URL values and verified TLS. Stateless AI services receive no databases. Runtime credentials are service-specific; local owners can migrate only their own database. Production should split migration and runtime roles.

Kafka carries committed facts through transactional outboxes. At-least-once delivery is assumed. Consumer inboxes and business uniqueness constraints prevent duplicate effects. Outbox retries are persistent and capped, with failed rows retained for operator recovery. Poison records go to a dead-letter topic before acknowledgment; transient exhaustion stops processing without acknowledging. Durable grading events can reconstruct analytics within broker retention; long-term archival is an operational prerequisite.

MinIO stores original content; content metadata and extracted text remain Content-owned. No vectors or indexed events are emitted. Future pgvector belongs to Content; version embeddings by model, dimensions, chunker and source revision, backfill a separate index, validate coverage, then switch atomically.

AI is deferred deliberately. Four independently authenticated Python applications validate capability-specific contracts and return explicit 501 errors. They never fetch private content or fabricate output.

Version evidence: Angular compatibility https://angular.dev/reference/versions; Kafka Docker https://kafka.apache.org/41/getting-started/docker/; Kong OSS JWT https://developer.konghq.com/plugins/jwt/. Patch versions are pinned in lockfiles/images; updates require rerunning checks.
