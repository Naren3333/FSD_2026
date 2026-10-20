# RAG quality follow-up — 2026-10-20

The owner's manual runs establish the baseline after the efficiency changes:

- Retrieval: `.local/eval-runs/20261020-223459-manual.json`, 275 executed queries, 223 scored, hit@1 0.924, hit@3/5 0.978, MRR 0.950, zero detected leaks. The unsupported topic filter case was skipped.
- Chat: `.local/eval-runs/chat-20261020-224443-manual.json`, 103 cases / 107 turns, three failed cases and six review flags. Failures: `waterfall-weakness`, `multi-review-testing`, `multi-persistence-design`. Refusal-case answers flagged for review are not automatically hallucinations.

## Changes

Adaptive topic-search reuse requires all meaningful query terms in the passage's title/section heading and a score above the existing relevance floor. A passing mention in body text no longer skips a dedicated topic search. This can increase retrieval calls for multi-topic questions; it trades a bounded amount of retrieval spend for better evidence coverage.

Duplicate passages retain every qualifying topic-search label, even when the whole-question search selected them first. Context fitting prioritizes one candidate per topic, then fills remaining space and restores retrieval order. Passage and token limits remain enforced.

After streaming, a local citation-coverage check adds an explicit notice when a requested topic has no cited passage from its search leg. This adds no paid model call. It is a structural check, not a semantic entailment checker: it does not prove that a cited source supports each claim, and it cannot retract text already streamed.

`query_rewrite.v3` permits concise subject/intent wording while forbidding guesses about unnamed subjects. The Waterfall question does not name Waterfall; the rewrite improvement is a candidate fix that still needs model evaluation. The global relevance floor remains 0.61.

Both evaluation CLIs accept repeatable `--case ID` filters and save atomic checkpoints after completed work. The chat runner prints progress for every completed case and retains completed cases if a worker fails. Incomplete reports explicitly carry `running`, `incomplete`, or `interrupted` status; they are not complete evaluation passes. The in-flight case may still incur charges even if it has not completed or been checkpointed. Golden holdout cases still require `--holdout`.

## Visual expectation review

The source pages were rendered and inspected. Basic Security p16 contains a genuine password-salt flow diagram as well as text and a comparison table. Automation and Deployment p9 contains a colored responsibility matrix. Both failing kind expectations remain unchanged. Correct-page text/figure retrieval does not satisfy the requested figure/table kind. Extraction/classification work and any reindexing remain outstanding; these retrieval/chat changes do not establish a fix for either kind failure.

## Targeted paid verification for the owner

Rebuild the chatbot before checking its new behavior:

```powershell
docker compose up -d --build --no-deps question-chatbot-agent
python scripts/eval/run_chat.py --case waterfall-weakness --case multi-review-testing --case multi-persistence-design --label targeted
```

Review the saved answers, citations and coverage notices. Run neighboring multi-topic cases before accepting a general quality improvement. Reserve holdout retrieval for final evaluation rather than tuning. The owner retains the $5 aggregate cap across all paid work; preserve both development and test ledgers, and check remaining allowance before paid runs. No automatic full paid rerun is part of this follow-up.

## Verification

`scripts/check.py --keep-going` passed all 15 lanes (112 seconds), including the chatbot's types/tests and evaluator tests. Log: `.local/rag-quality-check-final.log`. Focused tests exercise body-only topic mentions, duplicate provenance, missing citations, false citation markers in generated topic labels, tight context budgets, case selection, atomic checkpoint failures, and interrupted runs. Database-specific tests skip in the local gate; those suites and browser E2E were not rerun for this follow-up.

The complete real infrastructure integration rerun passed (355 seconds), including auth, classroom isolation, chat/API workflows, durable event replay, purges, database isolation/migrations, protected metrics, 501 boundaries and rate limits. Log: `.local/rag-infrastructure-final.log`. Provider keys were explicitly disabled in the isolated test stack, so this does not validate model answers. Existing test volumes and the usage ledger were reused and retained, and the test stack was stopped afterward. The first run failed its late metrics authorization assertion after 313 seconds; the harness now obtains fresh person tokens for late role checks and reports returned statuses on failure. No production authentication checks were relaxed.

The development chatbot container was rebuilt and restarted, and `scripts/wait_ready.py --timeout 60` passed for all ten applications and the gateway/Keycloak public ports. Existing development data and spend settings were retained. No paid evaluation was invoked by this follow-up. Production readiness and improved model quality are not claimed from unit tests.
