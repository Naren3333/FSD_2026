# Initial inspection and implementation plan

The initial checkout contains only `.git`, with no commits, source, README, migrations, configuration, tests, or AGENTS.md. There is no legacy implementation or data to migrate. The supplied brief is preserved in `docs/requirements.md`.

1. Record ownership and security decisions, then provision reproducible local infrastructure.
2. Build five independent Go transactional applications and five independent Python applications. Share only infrastructure helpers; keep business types and rules local.
3. Implement classroom authorization, curriculum, text uploads, immutable published assessments, student submissions, deterministic draft grading, explicit teacher finalization, and evidence-based analytics.
4. Use PostgreSQL transactions with outboxes, Kafka consumer groups, durable deduplication, bounded retries, and dead letters.
5. Define validated, authenticated AI contracts returning 501. No inference or invented data.
6. Connect Angular to gateway APIs with OIDC authorization-code/PKCE login.
7. Execute available checks, record unavailable checks honestly, and review security boundaries.

Initial environment: Go 1.27.1, Python 3.13.5, Node 22.17.0 are installed. Docker CLI exists but the daemon was not running at inspection. Container integration verification depends on starting the daemon successfully.
