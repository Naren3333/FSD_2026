# ADR 0005: Marking-scheme passages as context for AI marking suggestions

Status: accepted 2026-10-17. The project owner chose the teacher-picked passages option over Grading fetching them at marking time and Assessment fetching them at publish. Partly superseded 2026-10-18 by ADR 0009: marking schemes are no longer uploaded or searched; the passages stay as marking points the teacher writes, and the event and Grading parts below still hold.

## Context

ADR 0004's marking suggestions read only the question, its rubric (or a short answer's accepted answers) and the optional model answer. Teachers can already upload a classroom's marking scheme to Content (`doc_type` `marking_scheme` or `rubric`), indexed and searchable by the classroom's teacher only, but nothing tied it to a question. ADR 0004 listed it as not built.

Grading's marker runs in the background as the `grading-marker` service account, with no teacher token. It cannot call Content's search the way the chatbot does (with the user's own token), and it must not read Content's database (AGENTS.md).

## Decision

**The teacher picks the passages; they travel with the question.**

- A short answer or free response may carry `marking_scheme`: up to 5 passages, each with 1–2,000 characters of `text`, and at most 20 in an assessment. A passage picked from search also names its `document_id`, `chunk_id` and `page`; a pasted one may name none. Other question types refuse the field.
- In the editor the teacher chooses one of the classroom's indexed marking schemes or rubrics, and the web app calls Content's existing `POST /v1/search` with the teacher's own token, limited to that document, with the question's wording as the query. The teacher adds the passages that apply, or pastes one (for a text upload, which is not indexed, or when search is unavailable). The search is an embedding and a rerank, billed to the teacher like any search (ADR 0003).
- The passages are part of the question: saved on the draft, frozen when it is published, and never in the student projection, like the rubric. The ids are references the teacher chose, not authorization: no service fetches anything by them.

**The event carries them: `assessment.submitted.v3`.**

- v2's question schema forbids extra properties, so the passages need a new version. Assessment writes v3 only. Grading consumes v1, v2 and v3, each with its own consumer group, and is ready only while all of them run. v1 and v2 events still on Kafka are graded as before, without passages.
- The topic carries teacher-only text, like the answer keys and rubrics already in it, and needs the same production ACLs.

**Grading gives them to the marker as context.**

- The marking prompt adds `marking_scheme` (the passages' text) to the task document. The instructions say they are the teacher's own scheme for the question, to be used with the rubric, with the rubric's levels deciding where they disagree, and never quoted to the student.
- The checks of ADR 0004 are unchanged; feedback that repeats a sentence of a passage (25 characters or more) is withheld, as for the model answer.
- A suggestion records `scheme_passages`, how many passages the model was given. Grading review shows it with the passages. Students never see suggestions or passages.

## Alternatives not taken

- **Grading searches Content at marking time** through a new service-only route that lets `grading-marker` read a classroom's teacher-only documents on the teacher's behalf. Retrieval would be automatic, but it is a new kind of service authority over teacher-only data, every job pays for a search, and two students could be marked against different passages after a re-upload.
- **Assessment searches Content when the teacher publishes.** Automatic, but the teacher never reviews what the AI will read, and Assessment would depend on Content and ai-core.

## Consequences

- `kafka-init` creates `assessment.submitted.v3` and `assessment.submitted.v3.dlq`. An existing local setup needs only `docker compose up -d --build`; no migration, since questions are stored as JSONB in Assessment and Grading.
- A marking call grows by up to 10,000 characters of context per question.
- Not built: a second opinion for low-confidence suggestions, teacher-agreement metrics, and searching text uploads.
