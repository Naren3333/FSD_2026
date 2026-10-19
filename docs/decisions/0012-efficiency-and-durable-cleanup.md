# ADR 0012: Efficient retrieval and durable cleanup

Status: accepted 2026-10-19. The project owner authorized all eight improvements from the code review, including PDF processing.

## Decision

- Content deletes database records and writes both the deletion event and an object-cleanup job in one transaction. Its own worker retries object deletion after commit. Classroom purges use the same queue. Originals are never removed before the database deletion commits.
- ai-core first examines PDF text and layout locally. Clean text uses the existing parser; scanned pages, complex graphics and uncertain tables retain selective vision. A full-vision mode remains available for comparison and recovery. Source pages and PDF/vision provenance remain intact.
- Operational caches reuse versioned extraction/chunking and exact embedding inputs. Cache misses and failures never manufacture successful output. Concurrent identical requests share work within one process. Business data and access decisions remain with Content.
- Chat avoids unnecessary rewrites for unambiguous standalone questions, retains conversation-aware rewriting and topic coverage, and uses a token budget for evidence. Retrieval thresholds are not applied to incomparable RRF scores.
- ai-core reserves estimated spending atomically in its own database before provider work, settles actual/estimated usage, and shares concurrency enforcement across replicas. No transaction stays open during provider calls. Abandoned reservations expire conservatively and are observable.
- An optional `AI_TOTAL_SPEND_LIMIT_USD` serializes a ceiling across all users, organizations and retained usage/reservations in that ai-core ledger. It has no daily reset. The owner authorized at most $5 across this verification run; its isolated ledger uses a $4 ceiling with margin, and is retained across retries so resetting test data cannot reset this run's budget.
- Lists gain bounded pagination and batch reads without widening authorization or exposing answer keys. Existing shared Angular state and pagination primitives own loading, failure, empty, keyboard and narrow-screen behavior.
- Pool limits become validated configuration. Operational measurements expose real cache, queue, pool and stage behavior without recording prompts, tokens or student identities as metric labels. No new business service, Redis, sharding or framework is introduced.

## Implementation and verification sequence

1. Durable Content cleanup, including rollback, retry and duplicate-event database tests.
2. Text-first selective PDF processing, versioned reuse and chunk/context configuration; regression tests and an offline comparison on the four supplied decks.
3. Shared AI budget reservations and per-call accounting, including concurrency and recovery tests against PostgreSQL.
4. Chat orchestration and query coalescing, preserving refusals, citations and multi-topic coverage.
5. Paginated/batched APIs and Angular data loading; regenerate contracts and test authorization and UI states.
6. Operational measurements, pool configuration and reproducible benchmark tooling.
7. Run scripts/check.py and the disposable real infrastructure integration/database/E2E suites; record actual results and remaining limitations in the handover.

## Consequences

Logical deletion is immediate; physical object cleanup is asynchronous and retryable. Existing data is preserved by additive migrations. Cache versions must change when extraction semantics change. Local caches are optimizations, not authorities. Benchmark results describe the machine and corpus measured and do not establish production capacity.
