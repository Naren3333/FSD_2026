# ADR 0023: Model profiles and reasoning

Status: accepted 2026-11-22. The project owner approved the reliability plan for marking, chat and question generation, and this ADR is its shared ai-core part. It amends ADR 0002 and ADR 0003.

## Context

Every model call ran `gpt-6-luna` with reasoning off (`CHAT_REASONING_EFFORT=none`). The marking critic (ADR 0020) and the question reviewer (ADR 0015) used the same model and settings as the writers they check, so their errors tend to match. A structured reply that ai-core rejected ended the work, with no second attempt. A provider timeout was never retried, and one slow reranker reply made content fall back to unranked results. The ledger recorded what a call cost but not how it ended or how long it took.

## Decision

**Model profiles.** `POST /v1/inference` takes `model_profile`:

| Profile | Model | Reasoning effort | Used for |
|---|---|---|---|
| `default` | `CHAT_MODEL` (`gpt-6-luna`) | `STRUCTURED_REASONING_EFFORT` (`low`) | writers' structured and plain replies |
| `check` | `CHECK_MODEL` (`gpt-6-luna`) | `CHECK_REASONING_EFFORT` (`medium`) | a critic's or reviewer's second opinion |
| `fast` | `CHAT_MODEL` | `CHAT_REASONING_EFFORT` (`none`) | short rewrites (the chatbot's query rewrite) |

- Streamed chat keeps `CHAT_MODEL` at `CHAT_REASONING_EFFORT` (`none`) and takes only `default`. Any other profile on a stream is a 422.
- The ledger row, the reservation and the response's `model_version` name the model that was actually called.
- `docker-compose.yml` pins the three new settings. No caller uses `check` yet. The plan's later phases move the marking critic and the question reviewer onto it.
- `gpt-6.2-sol` is priced at $2.00 input, $0.10 cached input and $10.00 output per million tokens. That is OpenAI's standard short-context rate, checked on developers.openai.com/api/docs/pricing on 2026-11-22. Our prompts stay far below the 272K-token long-context tier.

**Reasoning headroom.** `max_completion_tokens` counts reasoning tokens as well as the reply. ai-core sends the caller's `max_tokens` multiplied by the effort's allowance: none ×1, minimal ×1.25, low ×1.5, medium ×2.5, high ×4. An unknown effort gets ×4. The result is never below `max_tokens` and is capped at 8000, the contract's largest value, which also bounds cost and time. The budget reservation (`budget.guarded`), a timeout's charge and the generation time budget all use the value sent, so the reservation still covers the worst case. A caller asking for 8000 tokens gets no extra headroom.

**Retries.**
- A timed-out attempt is retried once, but only for calls that have a deadline (generate and opening a stream). It is retried only if the time left still fits a whole attempt (`MIN_ATTEMPT`, or one full read timeout for a stream).
- Each attempt is given all of the time that remains, so a timeout usually leaves no room and the retry rarely fires. It helps when the provider fails early.
- Embeddings and the ingest's vision calls still do not retry timeouts.
- Openai may still bill an attempt nobody waited for, so each timed-out attempt is charged as a timeout (ADR 0003), even when the retry answers.
- The reranker gets one retry after a connection failure, a timeout, a 429 or a 5xx. Both attempts must fit in 3 s (content waits 4 s). Each attempt waits at most 2 s, and a retry needs at least 0.5 s left.

**One re-ask.** `AgentClients.generate` in `learning_transport.agents` gains three keyword arguments:
- `profile`, sent as `model_profile`.
- `check`, a callback that returns the faults it finds in an output.
- `reask`, the number of follow-ups allowed (callers pass 1).

A reply that ai-core rejected (`AI_OUTPUT_REJECTED`) or that fails `check` is asked again: the original messages, then the faulty reply when there is one, then "your previous reply failed these checks: …; return a corrected reply."
- The re-ask is billed to the same payer and runs within the same deadline. One that would start with less than `MIN_CALL` left is not started.
- If the reply is still faulty, ai-core's `AI_OUTPUT_REJECTED` is raised as before, or `CheckFailedError` is raised with the last faults and output. `CheckFailedError` is an `UpstreamError` with the same code, so existing handlers treat it as an unusable reply.
- No agent adopts the re-ask yet. Later phases do.

**Ledger outcome and latency.** Migration `007_usage_outcome.sql` adds two nullable columns to `usage`: `outcome` (`ok`, `rejected`, `timeout`, `failed`) and `latency_ms`. The migration is additive: it adds no default and rewrites no rows, and the `CHECK` is `NOT VALID`, so it applies to new rows only.
- Inference and rerank charges fill both columns. `failed` is a stream the provider broke off.
- Ingest charges and earlier rows leave both null.
- Failures that cost nothing are still not recorded.

## Results

Measured 2026-11-22 on the desktop stack with real keys, both check models at medium effort:

| Eval | `gpt-6-luna` | `gpt-6.2-sol` |
|---|---|---|
| Reviewer probe, 3 passes (13 planted faults and 4 good drafts each) | 39/39 caught, 0 good drafts failed, reason named 97.4%, $0.0070 | 39/39 caught, 0 good drafts failed, reason named 100%, $0.0567 |
| Marking eval, 30 drafted cases | 30/30 agreed with the expected levels, critic doubted none, $0.011 | the same, $0.101 |

`gpt-6-luna` stays the `check` default: it caught the same faults at about a ninth of the cost. Neither set yet has a case the first marking gets wrong, so the critic's true-doubt rate is unmeasured; a harder marking set is needed to separate the two models. With low reasoning on the writers, marking took p50 9.5 s (max 18.6 s) per suggestion including its review, and chat answers p50 3.1 s with the support check. Question generation with the review on took p50 41 s (8.8 s without it), and practice sets p50 31 s (ADR 0026).

## Consequences

- Structured replies on `default` now use reasoning tokens. A reply costs more and takes longer than it did at `none`, and up to 1.5 times the reservation is held while it runs.
- Upgrade: `docker compose up -d --build`. ai-core applies migration 007 on start. No `.env` change is needed. An ai-core built before this change rejects `model_profile` values other than `default`, so update ai-core before any caller that sends `check` or `fast`. The chatbot now sends `fast`.
- A re-ask adds two messages to the original ones, so a call that already sends 49 or 50 messages exceeds ai-core's 50-message limit.
- Not built: per-profile spend reporting and a profile for vision calls.

## Amendment: a call's own reasoning effort

Approved 2026-11-22 with the plan to speed up question generation.

- `POST /v1/inference` takes an optional `reasoning_effort` (`none`, `low` or `medium`). The call runs at that effort instead of its profile's, on the profile's own model.
- The reasoning headroom, the budget reservation, a timeout's charge and the generation time budget all follow the effort actually sent. The ledger still records the model.
- A stream takes only `none`, or no override. Any other value on a stream is a 422, as other profiles are.
- `AgentClients.generate` takes `reasoning_effort`. It is checked against the same three values, and every re-ask of the call uses it too. Without it, the body carries no `reasoning_effort` and the profile's effort applies as before.
- Update ai-core before any caller that sends `reasoning_effort`. An older ai-core rejects the unknown field.
