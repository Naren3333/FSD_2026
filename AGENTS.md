# Engineering boundaries

- Read docs/requirements.md, docs/architecture, and the current handover before changes.
- Exactly ten business applications; adding another requires an ADR and user authorization.
- Keep domain rules inside their owning service. Never access another service's database. Shared packages may contain transport, authentication and operational helpers only.
- Go: standard HTTP, explicit SQL/pgx, slog; Python: FastAPI/Pydantic, Ruff, mypy, pytest; browser: strict Angular only.
- Preserve committed data. No destructive migrations without explicit user authorization.
- Verify JWT issuer/audience/expiry and resource membership. Never trust identity headers or enable production auth bypasses. Never commit generated local credentials.
- Publish business events through a transactional outbox; acknowledge consumers only after durable processing or durable dead-letter delivery. Test duplicate business events.
- Grades remain drafts until an authorized teacher finalizes them. Never expose answer keys to students.
- AI contracts return 501 until real implementation is authorized. Never manufacture questions, citations, embeddings, metrics or successful placeholder output.
- Future retrieval uses authorized Content APIs. Agents have no direct access to business databases.
- Run scripts/check.py and applicable real infrastructure integration/E2E tests. Report checks that could not run. Do not claim production readiness.
- UI changes follow DESIGN.md and UX-CONTRACT.md; preserve loading, empty, failure, keyboard and narrow-screen states.
