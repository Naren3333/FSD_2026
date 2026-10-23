# ADR 0014: Performance interpretation agent

Status: accepted 2026-10-23. The project owner asked for the performance interpretation agent to be built as the first agentic workflow (the list is in `docs/architecture/agentic-workflows.md`), keeping clear of the retrieval work other contributors own.

## Context

Teachers see each student's topic accuracy under Learning performance, but turning that into "what is going wrong and what do I do next" is left to them. `student-performance-agent` was a 501 placeholder whose contract expected a caller to hand it a summary and evidence ids; nothing called it. Every AI feature so far is a fixed pipeline: one structured model call over inputs chosen in code. Interpreting results is different: which answers are worth reading depends on what the summary shows, so the model should choose what to look at.

## Decision

**The agent is a bounded tool-using loop, stateless, published by Kong at `/api/performance-agent` and called with the teacher's own token.**

- `POST /v1/performance_interpretation` takes a classroom and a student. Only the classroom's teacher may call it, and only for a student enrolled in it (checked at Identity); students and service accounts get 403. The old request contract (summary, evidence ids and topics supplied by the caller) is replaced: the agent gathers the evidence itself, so a caller cannot hand it invented data.
- It opens with the student's topic summary from Analytics (released grades) and their finalized evaluations from Grading (with the new `student_id` filter on `GET /v1/evaluations`), joined with the teacher's view of the assessments from Assessment's batch read and the topic names from Curriculum. A student with no finalized results gets `422 NO_EVIDENCE` without a model call. Draft grades and AI marking suggestions are never shown to the model.
- Then up to five model turns (`PERFORMANCE_MAX_TURNS`). Each is one structured ai-core `generate` call as the `student-performance-agent` service account, billed to the teacher (ADR 0003). A turn either calls up to three tools or reports; the last turn, or any turn after two minutes (`PERFORMANCE_DEADLINE`), may only report. The tools are read-only:
  - `read_answers` shows one result's questions (lost marks first, at most eight) with the prompt, expected answer, the student's answer, the rubric scores and the teacher's feedback.
  - `prerequisites` reads a topic's prerequisites from Curriculum with the student's evidence on each.
  - `class_comparison` pools the class's released topic accuracy from Analytics (its first 200 students) next to the student's.
- The model refers to results, questions and topics by short references (`R1`, `E1`, `T1`) the agent assigns; it never sees or writes ids. A refused tool call (an unknown reference, a repeat, an unknown tool) or a failed one is told to the model and shown to the teacher, and the loop goes on.
- The agent does not search Content or call the chatbot: it reads only assessment, grading, analytics and curriculum data. Linking an observation to teaching material is left for later, once the retrieval work settles.

**The report is checked against what the agent gathered; failures are dropped and counted, never repaired.**

- Every observation must cite at least one question the agent was shown. A possible misconception must cite only questions that lost marks and whose answers the model read with `read_answers`; marks alone are not enough to name a wrong idea.
- Recommended topics must be topics the agent was shown. At most five observations, three misconceptions, four interventions and three topics are kept.
- When no observation survives, the request fails with `422 INTERPRETATION_REJECTED`. The response lists the cited questions (assessment title, prompt, marks), every tool step with its reason and outcome, the number of model turns, and what was dropped by reason. It is marked `requires_teacher_review` and nothing is saved.

**The page keeps the teacher in charge.** Learning performance shows "Interpret results" once a teacher has opened a student's evidence. The draft shows each statement with the questions it rests on, and "How the agent worked" lists the agent's steps. Students do not see it.

## Consequences

- An existing local setup needs `python scripts/upgrade_local.py` (adds `STUDENT_PERFORMANCE_AGENT_CLIENT_SECRET` and the Keycloak client), then `docker compose up -d --build` and `docker compose restart kong` for the new route.
- Without provider keys the request answers `503 AI_PROVIDER_NOT_CONFIGURED`, which the page shows.
- An interpretation costs up to five model calls. They are bounded by the turn limit, the deadline and the tokens per turn, and the teacher's daily allowance applies to each.
- The summary comes from released grades while the results include finalized grades not yet released, so with manual release the two can differ until the teacher releases them.
- Not built yet: running it automatically when a grade is finalized (it would need storage and a payer), saving interpretations, linking findings to teaching material, generating practice from the recommended topics (with a question bank), and evaluating the reports against teachers' own judgement.
