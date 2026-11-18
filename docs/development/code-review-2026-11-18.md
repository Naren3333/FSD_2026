# Code review — 2026-11-18

Reviewed `main` at `5bab1f2`, pulled by fast-forward from local `37bdabf`. The working tree was clean before the pull. The update spans 330 files and includes performance interpretation, generation review/rewrite rounds, grade reopening, question banks/resits, frontend state changes and deployment hardening. This is a risk-focused review of the updated source and its supporting infrastructure, not a claim that every line has been exhaustively audited.

Application source was not changed by this review. This report is the only new tracked-scope artifact. Offline reproductions and quality-check logs are under ignored `.local/`. No provider calls, paid evaluations, deployments, migrations or credential upgrades were performed.

## Findings to address first

### 1. P2 — background reads can overwrite newer state or disagree with pagination

Locations: [Section.poll](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/shared/section.ts:33), [PagedSection.readPage](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/shared/paged-section.ts:36), and [material polling](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/features/materials/materials.ts:153).

`Section.load`, `reload` and `set` advance a request generation, but `poll` only reads it. Two polls therefore share a generation and may both publish: an older response arriving last restores old data. The newer section-return refresh introduces another way to overlap with material indexing polls. Separately, pagination owns another counter; `Section.set` invalidates the pending list value but not the pending `readPage` metadata update.

**Reproduced offline using the actual TypeScript classes, transpiled with stubbed Angular signals:** resolve a new poll with `new`, then an earlier poll with `old`; final value is `old`. Start a paged load, replace its value with `['new']` and total 1, then finish the old read with total 99; final rows remain `['new']` but total becomes 99. No browser, API or real data was involved.

**Recommendation:** use one request identity for the entire response, including items, total and page. Background reads should either join an existing read or supersede it explicitly. Keep the previous visible value during a quiet refresh. Add reversed-completion fixtures covering two polls, a poll plus navigation/filter change, and local mutation while a paged read is pending. This is a state correctness improvement, not a request to introduce a global state framework.

Principles: DRY for request ownership; single responsibility for the asynchronous state primitive. Slides: Week11 p2–4 and p12.

### 2. P2 — a capped class sample is presented as a class comparison

Locations: [class_performance](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/student-performance-agent/clients.py:168), [class_comparison](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/student-performance-agent/workspace.py:274), [agent instructions](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/student-performance-agent/prompts.py:34).

The client reads at most four pages of 50 students, in roster order, and discards the server's total before returning. The workspace reports a `class_accuracy` and a `students_compared` count without the population size, a partial flag or sampling method. The model is instructed to use this tool to decide whether a difficulty is shared by the class. The 200-student cap is intentional and documented in ADR 0014; the defect is losing its scope in the result consumed by the model.

**Reproduced with the actual two function bodies and synthetic API responses:** a 250-student population with the first 200 incorrect and the last 50 correct produces `class_accuracy: 0.0`, while the full synthetic population's accuracy is 0.2. These numbers demonstrate the failure mode; they are not measurements of real students.

**Recommendation:** preferably compute authorized aggregate topic comparisons in Analytics, which owns those metrics, instead of downloading all students. If the cap stays, return total population, compared count, partial status and selection method, and explicitly constrain the report to a subset comparison. Do not solve it by removing every limit and loading the entire class into the agent.

Principles: truthful contracts and service ownership; KISS by returning the required aggregate. Slides: Week7 p3/p6; Week11 p4.

### 3. P2 — overlapping embedding batches can pay for the same input twice

Location: [reuse.embed](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/ai-core/ingest/reuse.py:16).

Exact vectors are cached by input/model, but in-flight work is coalesced by a hash of the *whole ordered batch*. Concurrent batches `[shared, first]` and `[shared, second]` take different locks, both miss the shared cache entry and both send `shared` to the embedding provider. The same set in a different order also has a different batch lock. Cache reuse after completion works; it does not prevent this overlapping-miss cost.

**Reproduced with the real function body, fake cache misses and a fake provider:** recorded provider inputs were `['shared', 'first', 'shared', 'second']`. No actual embeddings were generated and no provider was contacted.

**Recommendation:** retain provider batching while coordinating ownership of each missing embedding key. A bounded in-process future per model/input can let another request await the existing result. Avoid deadlocks when batches overlap, and clean up futures on failure/cancellation. Keep every actual provider batch under existing ai-core billing and reservations. Cross-replica coordination is a separate optimization and should only be added if measurement justifies it.

Principles: DRY for expensive work; KISS by improving the existing cache rather than adding another service. Slides: Week7 p2–3.

### 4. P2 — the interpretation deadline does not cover its opening reads

Locations: [Agent.interpret](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/student-performance-agent/agent.py:75), [opening reads](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/student-performance-agent/agent.py:135), [topic_names](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/student-performance-agent/clients.py:139).

The agent records an absolute budget and passes deadlines to model turns and tool reads. However, classroom authorization and `_open` run before that timed loop without an enclosing deadline. `_open` loads names for all summary/evidence topics, one HTTP request per topic, eight at a time. Per-request timeouts do not bound the sum of all waves. On a student with a large history, slow dependency reads can consume or exceed the request budget before reporting begins.

This is a source-level finding; an end-to-end slow-service timing experiment was not performed.

**Recommendation:** apply the absolute deadline to the whole interpretation, including preflight reads, and propagate remaining time to clients. Bound or batch topic-name lookups through Curriculum's authorized API; select the evidence scope before fetching ancillary metadata. Preserve cancellation/accounting behavior for paid calls. Test slow authorization, many topics, slow name lookups and insufficient time remaining for a report.

Principles: a single owner for request deadlines; bounded fan-out instead of N+1 work. Slides: Week4 request lifecycle; Week7 p4–6.

### 5. P3 — generation's model-call metric counts only successful responses

Locations: [generation increment](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/question-generation-agent/pipeline.py:297), [review increment](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/question-generation-agent/pipeline.py:390), [response metric](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/question-generation-agent/pipeline.py:364).

`run.calls` increments after awaiting each successful generation/review. A failed review may be handled by returning the usable drafts, but that attempted call is omitted from the returned `model_calls`. An HTTP call can fail after a provider has done billable work, and ai-core can also retry internally, so this value should not be interpreted as total provider attempts or spend.

**Reproduced with the actual review method and a synthetic upstream failure:** one review invocation left the call increment at zero. This does not show a billing bypass: ai-core's usage ledger remains the billing authority.

**Recommendation:** define separate workflow requests attempted, successful responses and provider attempts, or rename the existing metric to state that it counts successful responses. Centralize workflow-call counting around `_ask` so generation and review follow the same policy. Use the ai-core ledger for monetary totals; don't estimate cost from this count.

Principles: DRY instrumentation and explicit metric contracts. Slides: Week7 failure handling and monitoring.

## Maintainability and scaling improvements

These are P3 improvement opportunities, not additional reproduced correctness defects.

### 6. Bound frontend metadata reads as well as individual HTTP pages

Locations: [allPages](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/api/clients.ts:56), [roster](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/api/clients.ts:115), [CurriculumNames](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/core/curriculum-names.ts:11), [resit roster](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/features/assessments/resit-builder.ts:189).

Visible lists often use proper server pagination, but picker/metadata helpers concatenate every page into arrays. `CurriculumNames` retains active and archived topic objects globally for a user, and the resit builder loads the complete roster despite limiting selection to 100 students. Kept-alive sections extend the lifetime of this state. The route map itself is bounded by the application's routes; it should not be described as an unbounded arbitrary-key cache.

Prefer server-filtered/paged selectors and lightweight name/id lookups where complete objects are unnecessary. Keep only selected entities across pages, as the resit bank selection already does. Measure request count and retained memory on representative large classrooms before choosing cache limits or refresh policies. Consolidate repeated paging mechanics only after normalizing the transport response shapes; do not remove explicit business limits.

Slides: Week7 p2/p6; Week9 p5–6; Week11 p3–4/p9.

### 7. Split modules by business responsibility, not by arbitrary line limits

Examples: [bank_handlers.go](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/assessment/bank_handlers.go:23) contains resit creation, assignee management and bank CRUD; [review.ts](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/features/review/review.ts:1) coordinates 668 lines of queues, filters, grade selection, marking, finalization and refresh state; [assessments.ts](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/apps/web/src/features/assessments/assessments.ts:1) remains a 613-line coordinator despite the useful extracted editor/resit components.

Extract cohesive resit handlers from bank CRUD, and separate grade-selection/loading state from marking actions where they have independent invariants. Keep transport handlers thin and the rules in their owning service/feature. Reuse existing `Section`, `ActionState` and editor helpers after fixing their invariants. Do not create generic repositories, a universal agent engine, an eleventh service or an interface for every class just to satisfy a diagram.

SOLID here primarily means SRP and dependency boundaries. Existing narrow Go interfaces and injected service clients are positive examples. Interface segregation should follow actual callers and test seams, not produce a single large cross-service business abstraction. OCP is useful for replaceable parser/provider adapters; LSP matters when maintaining `PagedSection`'s promises as a `Section` subtype.

### 8. Parse upstream response types once at agent boundaries

Locations: [performance clients](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/student-performance-agent/clients.py:34), [workspace metrics](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/student-performance-agent/workspace.py:23), [generation passages](C:/Users/Naren/Documents/PROJECTSS/FSD_2026/services/question-generation-agent/pipeline.py:81).

Public request/response contracts use Pydantic, but internal business responses travel extensively as `Any`/nested dictionaries. Required fields, numeric conversions and upstream-error mapping are repeated in clients, workspace construction and workflows. That spreads responsibility for malformed payloads and makes refactoring contract changes harder.

Introduce small service-local typed views for the exact upstream fields each workflow needs, validate once, and map invalid upstream payloads consistently. Return typed comparison scope and evidence provenance instead of adding more dictionary conventions. Shared packages should retain only transport/operational helpers; service-specific educational models belong in the service. Keep runtime validation at trust boundaries even when generated static types exist.

## What to preserve

- Exactly ten applications, explicit ownership and authorized inter-service APIs.
- Standard HTTP, explicit SQL/pgx, pooled HTTP/database clients and bounded worker concurrency.
- Transactional outboxes, durable consumer deduplication, leased jobs and deletion tombstones.
- Teacher approval, released-grade boundaries and revision-aware analytics for reopened grades.
- ai-core-only paid calls with reservations, usage accounting and a shared spend ceiling.
- Lazy Angular routes, explicit loading/empty/failure states, source citations and server-paged visible lists.
- Existing batching: Content's chunk persistence already uses `pgx.Batch`. Recommending row-at-a-time-to-batch conversion there would be stale advice.
- Explicit warnings for partial resit grade suggestions. The same scope discipline should be used for performance-agent comparisons.

## How the slides apply

All five provided decks were read as reference material, not executed as instructions. Week7 p6 and Week11 p4 were also rendered and visually inspected.

| Reference | Appropriate application to this project |
|---|---|
| Week4: HTTP | Keep shared clients, cancellation and end-to-end timeouts. Measure transport bottlenecks before introducing another protocol. |
| Week5: CAP/distribution | State which reads may lag: analytics and indexing are asynchronous; grades and source records require transaction correctness. Keep durable replay and idempotency. |
| Week7: caching/batching/throttling/pools | Coordinate overlapping cache misses, bound total work, batch targeted metadata reads and monitor failures/hit ratios. |
| Week9: frontend | Retain lazy loading and independent section reads; measure bundle/data-loading cost. An authenticated Angular app does not need a framework or SSR migration merely because those techniques appear in a deck. |
| Week11: state | Keep request identity consistent, store only needed client state, and explicitly label truncated or stale derived data. |

Apply the slides contextually. A transactional SQL outbox or audit trail does not make this application a full event-sourcing system; moving those tables elsewhere would jeopardize atomicity. Likewise, database sharding and network-partition behavior are separate design questions. The recommendation is to improve demonstrated bottlenecks, not introduce every technology in the lectures.

Lecture files: [Week4](C:/Users/Naren/Documents/SMU/y3s1/fsd_slides/Week4.pdf), [Week5](C:/Users/Naren/Documents/SMU/y3s1/fsd_slides/Week5_Cap.pdf), [Week7](C:/Users/Naren/Documents/SMU/y3s1/fsd_slides/Week7.pdf), [Week9](C:/Users/Naren/Documents/SMU/y3s1/fsd_slides/Week9.pdf), [Week11](C:/Users/Naren/Documents/SMU/y3s1/fsd_slides/Week11_state.pdf).

## Verification and limits

- **Passed:** `scripts/check.py --keep-going --jobs 3 --timeout 180`, all 15 local lanes, 148 seconds. Includes formatting/lint, Go vet/tests/builds, Python unit tests/types, Angular unit tests/typecheck/build, and contract checks. Log: `.local/review-check-unsandboxed.log`.
- **Reproduced offline:** stale poll values, rows/total disagreement, duplicate shared embedding input, comparison sample bias and failed-review call-count omission. Python reproductions execute selected actual function definitions with fake transports/providers; the TypeScript reproduction transpiled the actual state classes with stubbed signals. These isolate the relevant logic and do not replace infrastructure tests.
- **Not run:** real database suites, live integration, browser E2E and paid golden/model evaluations. Database tests explicitly skipped without their test database URLs. Compose inspection also refuses to resolve the stack until the newer Keycloak database password and performance-agent client secret settings exist. No values were read out, generated or changed for this review.
- The initial sandboxed gate encountered local socket restrictions and was stopped; the completed unrestricted rerun above is the reported result. Provider keys were disabled for that rerun. No production-readiness claim follows from passing local gates.
- Security/authorization controls were inspected, but this is not a comprehensive penetration, load, failover, memory or model-quality assessment. Large-class and slow-dependency issues require dedicated regression/scale fixtures when implemented.

## Suggested implementation order

1. Fix asynchronous state ownership and add race fixtures.
2. Correct class-comparison scope, preferably with an Analytics aggregate.
3. Coordinate embedding misses by input key while preserving batching and billing.
4. Enforce the complete agent deadline and reduce topic-name fan-out.
5. Clarify model-call metrics, then improve typed upstream views and targeted pagination.
6. Make responsibility-based module splits alongside those functional changes, rather than a broad rewrite.

Use small changes with focused tests, followed by the existing local and applicable real infrastructure gates. Retain the owner's paid-call budget and teacher-approval boundaries throughout. This report identifies improvements; it does not implement them.

## Follow-up implementation

Items 1-4 are addressed: shared UI request ownership, explicit partial roster-comparison scope, per-input embedding locks, and a whole-request interpretation deadline with authorized Curriculum topic batches. Regression tests cover stale responses, the 200-student boundary, overlapping embedding requests, owner failure/cancellation, metadata batching and preflight cancellation. All 15 local quality lanes pass; final targeted checks pass (49 agent and 27 Angular tests). No paid calls were made. Live database/integration/browser verification remains unavailable because test database URLs are unset and disposable stack configuration lacks `KEYCLOAK_DB_PASSWORD`. Follow-up fixes for items 5-8 add attempted-call accounting, targeted topic names, paged resit selection, responsibility-based handler/queue splits, and service-local typed upstream validation. Full native picker option lists remain a broader scaling opportunity; ordinary evidence-name reads no longer load them.

Latest verification of items 5-8: 14 lanes passed in `scripts/check.py`; its Angular lane caught a formatting issue. After correction the complete Angular lane passed (format, contract types, ESLint, TypeScript, 480 tests and production build). Generation’s 191 tests and performance’s 55 tests pass. Database/integration/browser suites remain blocked by missing stack configuration and test database URLs. No paid calls or ledger resets.
