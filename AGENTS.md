# Engineering boundaries

- Keep explanatory comments necessary, brief and lowercase. Preserve case-sensitive directives and license notices.
- Read docs/requirements.md, docs/architecture, and the current handover before changes.
- Exactly ten business applications; adding another requires an ADR and user authorization.
- Keep domain rules inside their owning service. Never access another service's database. Shared packages may contain transport, authentication and operational helpers only.
- Go: standard HTTP, explicit SQL/pgx, slog; Python: FastAPI/Pydantic, Ruff, mypy, pytest; browser: strict Angular only.
- Preserve committed data. No destructive migrations without explicit user authorization.
- Verify JWT issuer/audience/expiry and resource membership. Never trust identity headers or enable production auth bypasses. Never commit generated local credentials.
- Publish business events through a transactional outbox; acknowledge consumers only after durable processing or durable dead-letter delivery. Test duplicate business events.
- Grades remain drafts until an authorized teacher finalizes them. Never expose answer keys to students (except keys of practice questions a student generates for themselves, ADR 0022).
- AI contracts return 501 until real implementation is authorized. Authorized so far: ai-core inference, ingest and rerank; Content retrieval; question-chatbot-agent grounded answering (ADR 0002); Grading marking suggestions (ADR 0004) and their second check by the marking critic (ADR 0020); question-generation-agent questions and marking guidance for teachers (ADR 0006) and self-practice questions for students (ADR 0022); student-performance-agent interpretations (ADR 0014) and class-insights answers (ADR 0021) for teachers. Never manufacture questions, citations, embeddings, metrics or successful placeholder output.
- Every paid AI call names the user it is billed to; ai-core records its cost and enforces each user's daily allowance (ADR 0003). Never add a model call that bypasses ai-core or its spend check.
- Future retrieval uses authorized Content APIs. Agents have no direct access to business databases.
- Run scripts/check.py and applicable real infrastructure integration/E2E tests. Report checks that could not run. Do not claim production readiness.
- UI changes follow DESIGN.md and UX-CONTRACT.md; preserve loading, empty, failure, keyboard and narrow-screen states.
