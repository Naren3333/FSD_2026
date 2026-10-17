# ADR 0003: Per-user daily AI spend caps

Status: accepted 2026-10-13 by the project owner, who set the limits: $5 a day per student and $10 a day per teacher.

## Context

Every chat turn, search and PDF index costs OpenAI and Voyage money. The only limit was Kong's rate limit of 120 requests per minute, shared by every user, so one student could run up the bill and lock everyone else out. Users also could not see what they had spent.

## Decision

- **ai-core keeps the ledger.** Every paid call passes through it. It knows the model and the provider's token counts, and the requirements (section 6, service 7) give the AI service "token and usage tracking".
  - It gets its own `aicore` database with one `usage` table: one row per billable call, with the user, the operation, the model, the tokens and the cost.
  - It still holds no business records and stores no prompts or answers.
- **Callers say who pays.** `/v1/inference` and `/v1/rerank` require `billed_to` (`user_id`, `org_id`, `role`), and `/v1/ingest` requires `billed_user`, `billed_org` and `billed_role`.
  - Only `ai-client` service accounts can call ai-core, and they take these values from the user's verified token.
  - The values decide whose allowance pays. They never decide what a call may read: that is still Content's access check, made with the user's own token.
- **Who is billed:**
  - The chatbot bills the user for the query rewrite and the answer.
  - Content bills the user who searched for the query embedding and the rerank.
  - Content bills the teacher who uploaded a document for indexing it.
- **The cap.**
  - Before a paid call, ai-core sums the user's spend since local midnight. If it has reached the limit, ai-core answers `429 AI_SPEND_LIMIT` with `spent_usd`, `limit_usd` and `resets_at`, and makes no provider call.
  - The limit is $5 for a student and $10 for a teacher. An account holds exactly one of the two roles; a token with both is refused.
  - The day starts at midnight in `SPEND_TIMEZONE` (default `Asia/Singapore`).
  - The limits are set by `SPEND_LIMIT_STUDENT_USD` and `SPEND_LIMIT_TEACHER_USD`.
- **Prices.**
  - The prices are a table in `services/ai-core/spend.py`, in USD per million tokens. They come from third-party price listings checked on 2026-10-13; the provider's own price page is authoritative. `MODEL_PRICES` overrides the table.
  - A model missing from the table is charged at the most expensive known rate, never for free.
  - A stream that ends without OpenAI's usage numbers is charged by estimate (about four characters per token), and the row is marked `estimated`.
- **No ledger, no call.** If the ledger cannot be read, ai-core answers `503 USAGE_LEDGER_UNAVAILABLE` and reports not ready.
- **Showing it to users.**
  - The chatbot's `GET /v1/usage` (through Kong at `/api/chatbot/usage`) asks ai-core for the caller's spend today.
  - The web app shows a meter beside the classroom picker.
  - A chat turn over the limit ends with an `AI_SPEND_LIMIT` error that says when the allowance resets.
  - Indexing over the limit waits until the reset without using up its retry attempts.

## Consequences

- The cap is checked before a call and recorded after it. Calls already running when a user reaches the cap still finish, so a user can go over by at most what those calls cost. For a large PDF that can be a few cents to a dollar.
- Query vectors that Content already cached cost nothing and are not charged.
- An existing local setup needs `python scripts/upgrade_local.py` once. That creates the `aicore` role and database and adds `AICORE_DB_PASSWORD` to `.env`.
- Spending is per user, not per organisation. An organisation-wide budget would be a second limit on the same ledger.
