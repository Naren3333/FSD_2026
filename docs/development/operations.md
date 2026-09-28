# Operations and recovery

## Health and logs

Each application exposes `/health/live` and `/health/ready` on its private port 8080. Persistent apps check PostgreSQL; Content also checks the document bucket; Grading marks readiness unavailable after its consumer stops; Analytics checks its database and consumer state. AI placeholders need only valid authentication configuration; no model-provider dependency or API key is required. A readiness probe is a dependency snapshot, not proof of fresh analytical results.

Go uses JSON slog and Python emits JSON application telemetry. Request IDs and correlation IDs are returned in headers; correlation IDs flow across authorization HTTP requests and event envelopes. Tokens, bodies and student answers are not logged. Third-party server/broker startup diagnostics may use their own format. Extend these boundaries with OpenTelemetry HTTP/Kafka propagation and trace exporters in a later milestone; no tracing collector is installed.

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
