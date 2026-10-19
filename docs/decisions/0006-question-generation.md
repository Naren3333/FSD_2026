# ADR 0006: Question generation and drafted marking guidance

Status: accepted 2026-10-17. The project owner asked for the plan in `plans/question-generation-design.md` to be built, with these decisions: teachers only, no numerical questions in the first version, and generation before a question bank.

## Context

A teacher who has uploaded a classroom's PDFs still writes every question, accepted answer and rubric by hand. The question-generation agent was a 501 placeholder with a contract nobody called. Marking-scheme passages (ADR 0005) help AI marking, but only when the teacher has a scheme and finds the right passage for each question.

## Decision

**The agent is a fixed, stateless pipeline, published by Kong at `/api/question-generation` and called with the teacher's own token.**

- `POST /v1/questions` takes a classroom, 1–10 of its documents, one curriculum topic (called a skill until ADR 0007), 1–3 question types (multiple choice, short answer, free response), a difficulty, a count of 1–10, an optional focus, and the prompts already in the editor. It returns drafts in Assessment's own question shape with `requires_teacher_review: true`. Nothing is saved.
- `POST /v1/marking-guidance` takes a question the teacher wrote (short answer or free response) and documents, and returns suggested accepted answers or a rubric and model answer, plus marking points written as a marking-scheme passage.
- Only the classroom's teacher may call either (checked at Identity); students and service accounts get 403. Student self-practice would need its own ADR.
- Each request: the topic is read from Curriculum and Content `POST /v1/search` runs with the teacher's token over the chosen documents (12 passages for questions, 6 for guidance). If the best reranked passage is below the relevance floor (`RELEVANCE_FLOOR`, default 0.5, calibrated on the eval corpus on 2026-10-19), the request fails with `422 INSUFFICIENT_SOURCE` instead of generating from too little. If reranking fell back to unscored order, the floor cannot be applied and the request fails with `503 RERANK_UNAVAILABLE`, as the chatbot's turn does. Then one structured `generate` call to ai-core as the `question-generation-agent` service account, billed to the teacher (ADR 0003: spend limit and AI_BUSY become 429).

**Every draft is checked before the teacher sees it; failures are dropped and counted, never padded.**

- A draft must cite passages it was given and quote evidence (at least 20 characters) that appears in one of them.
- Multiple choice: 2–8 distinct options with the key among them. Short answer: 1–20 accepted answers. Free response: a rubric within Assessment's limits; points are the sum of each criterion's top level.
- Near-duplicates of each other or of the editor's prompts are dropped. The response says how many were requested, returned, and dropped by reason; `422 GENERATION_REJECTED` or `GUIDANCE_REJECTED` when nothing passes.
- Generated short answers and free responses carry their marking points as one marking-scheme passage, so AI marking has context without a separate scheme.

**Provenance stays with the teacher.** Assessment questions gain optional `sources` (up to five passages) and `origin: "generated"`. The teacher sees them; students never do, and `assessment.submitted.v3` leaves them out, so the event schema and Grading are unchanged. JSONB storage needs no migration.

**The editor keeps the teacher in charge.**

- "Generate questions from teaching materials" lists drafts with their key, evidence, explanation and sources. "Add to assessment" puts a draft into the editor as an ordinary, editable question; "Discard" drops it.
- "Draft marking guidance" on a short answer or written response shows the guidance first. "Use this" adds the accepted answers, or replaces the rubric and model answer, and adds the marking points to the question's when there is room.
- Retired by ADR 0009 (2026-10-18): "Match scheme to questions" searched one uploaded marking scheme once per teacher-marked question and added the best passage, within five per question and twenty per assessment, in the browser through Content's search. Marking points are now written by the teacher or kept from drafted guidance.
- Marking stays per question: the whole scheme PDF is never sent to a marking call.

## Consequences

- An existing local setup needs `python scripts/upgrade_local.py` (adds `QUESTION_GENERATION_AGENT_CLIENT_SECRET` and the Keycloak client), then `docker compose up -d --build` and `docker compose restart kong` for the new route.
- Without provider keys, generation answers `503 SEARCH_UNAVAILABLE` or `AI_PROVIDER_NOT_CONFIGURED`; the editor shows the message.
- Not built yet: numerical questions, a question bank (it will reuse `sources` and `origin`), student self-practice, and measuring how many generated questions teachers keep. The relevance floor needs calibrating with real documents.
