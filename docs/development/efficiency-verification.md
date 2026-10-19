# Efficiency changes: verification record

Scope: all eight review improvements, including PDF processing, authorized by the owner. Architecture and recovery details are in ADR 0012 and operations.md. Exactly ten business applications remain. No destructive migration or production deployment is part of this work.

## Delivered changes

1. Content commits logical deletion, its event and a durable cleanup job together; leased workers retry object removal after commit. Classroom purges and late indexing crops use the same cleanup mechanism.
2. ai-core extracts native text locally and selects uncertain graphics/layout for vision. Full vision remains configurable for comparison and recovery.
3. Chat skips rewriting simple standalone definitions and searches additional topics only when primary reranked hits do not cover them. Authorization and relevance thresholds remain enforced.
4. Versioned preparation and exact-input embedding caches reuse unchanged work. Content coalesces identical query-vector misses within a replica.
5. PostgreSQL reservations serialize user allowance and concurrency across replicas before provider attempts; durable usage settles them. An optional aggregate ceiling covers all users and organizations in one ledger.
6. Additive bounded page APIs and authorized assessment batch reads reduce list/detail traffic. The UI retains its loading, failure, keyboard and narrow-screen states; dashboard roster reads match the visible page.
7. Configurable chunk size/overlap and slide boundaries, token-budgeted chat evidence, and reproducible offline comparison tooling support later golden-set tuning.
8. Protected operational metrics expose real request/cache/pool/queue/usage measurements. Validated pool configuration supports measurement before changing capacity.

## Offline PDF comparison

`services/ai-core/benchmark.py` ran without provider calls on the four supplied slides. The report is `.local/pdf-efficiency.json` (ignored local evidence).

| PDF | Pages | Pages selected for vision | Chunks at 600 tokens / 90 overlap |
|---|---:|---:|---:|
| Week4.pdf | 10 | 3 | 10 |
| Week5_Cap.pdf | 22 | 8 | 28 |
| Week7.pdf | 6 | 0 | 6 |
| Week9.pdf | 17 | 1 | 16 |

Selection routes 12 of 55 pages to the initial vision pass. This is a routing count, not a measured reduction in paid calls, price or answer errors: selected pages may use several model operations. The 400/600/800-token variants with zero or 15% overlap produce the same chunk counts on these short decks, so this corpus alone does not establish an optimal policy. Defaults remain 600/90 with landscape slide boundaries.

## Verification evidence

Real PostgreSQL tests passed in all six Go modules and all three Python database suites, including deletion rollback/retry/duplicates, student filtering, shared concurrency and aggregate cross-user reservation limits. Local logs: `.local/improvements-db.log`.

- `scripts/check.py --jobs 4 --keep-going`: all 15 lanes passed (150 seconds), including formatting, lint, types, unit tests, builds and contract regeneration checks. The expected database skips were covered by the real PostgreSQL run above. Log: `.local/improvements-check-final-pass.log`.
- Playwright: all eight tests passed (2.4 minutes), including real-material pagination, keyboard Next, failed-read recovery, deletion from the final page, and mobile overflow checks. Narrow-screen Materials and performance screenshots were inspected. Log: `.local/improvements-e2e.log`; screenshots under `apps/web/test-results/` (ignored).
- The first live integration run passed through classroom/account deletion and purge, then caught stale explicit migration expectations. These were updated to include Content 005 and ai-core 003. The complete `scripts/integration.py --live-ai` rerun passed (124 seconds), including database isolation/migrations, service accounts, protected metrics, 501 boundaries and per-session rate limits. Log: `.local/improvements-integration-final.log`.

The installed premium UI audit ran in strict mode and returned 64 findings: 55 Angular event-handler detection warnings and nine inline-textarea-style warnings. Angular `(click)` is missing from its action regex; resize is mostly governed by shared CSS, with an existing student-answer override allowing vertical resizing. It is not reported as a clean automated audit pass. Local evidence: `.local/improvements-ui-audit.json`.

## Spending and deferred evaluation

The owner authorized at most $5 total for this verification. The isolated ai-core ledger started with zero usage and uses `AI_TOTAL_SPEND_LIMIT_USD=4`, leaving margin below that cap. Its usage database is retained across retries; no fresh-stack reset occurs after enabling paid work. Only ai-core makes provider calls. Recorded prices were checked against the official [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini), [embedding model](https://developers.openai.com/api/docs/models/text-embedding-3-small) and [Voyage pricing](https://docs.voyageai.com/docs/pricing) pages. Ledger amounts are configured-price accounting, not an external account invoice or unrelated account traffic.

Final recorded cost: **$0.00061094**, across 12 ledger charges, 3382 input tokens and 549 output tokens; outstanding reservations: **$0**. Aggregate evidence is `.local/improvements-spend.json`. ai-core was stopped before the final snapshot, and the isolated test stack was stopped after verification, retaining its volumes and ledger. Restarting/recreating it for another paid run must preserve that ledger and the aggregate cap rather than starting a fresh budget.

The owner explicitly deferred golden RAG evaluation. Definitions exist in `scripts/eval/golden.yaml`, `chat_cases.yaml` and `corpus.yaml`; their 22 source PDFs are missing locally. Existing historical golden results predate these changes. Restore the exact corpus and compare full/selective vision, chunk policies and adaptive routing with the same golden queries before claiming improved retrieval quality or actual savings. The four supplied decks cannot substitute for that corpus's expected-page labels.

Production readiness, load capacity, a comprehensive accessibility audit and dependency-security remediation are not claimed. `npm ci` reported 36 dependency vulnerabilities (2 critical, 24 high); forced dependency upgrades were not included in this efficiency change.
