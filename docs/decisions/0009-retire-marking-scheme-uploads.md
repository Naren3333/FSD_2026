# ADR 0009: Retire uploaded marking schemes, and let teachers delete materials

Status: accepted 2026-10-18. The project owner found the marking-scheme workflow finicky and chose to drop it: teachers write how a question is marked themselves (or keep what AI question generation drafts), and assessments can still be generated. In the same change, teachers can delete an uploaded material. Supersedes the upload, search and matching parts of ADR 0005 and the "Match scheme to questions" part of ADR 0006.

## Context

ADR 0005 let a teacher upload a classroom's marking scheme or rubric to Content (`doc_type` `marking_scheme` or `rubric`), search it from the editor and attach passages to a question as AI marking context. ADR 0006 added "Match scheme to questions". In use, matching a scheme's passages to questions was unreliable and hard to check, while the per-question rubric, accepted answers and model answer already carry what AI marking needs. Teachers also had no way to remove an uploaded document other than deleting the whole classroom.

## Decision

**Content takes teaching material only.** `POST /v1/documents` refuses `doc_type` `rubric` and `marking_scheme` with 422; `doc_type` stays optional and accepts only `teaching_material`. Documents uploaded earlier keep their type, stay teacher-only (students never see them, in lists, originals, figures or search) and can be deleted. Their rows are not changed: no migration.

**ai-core has one chunking path.** A separate marking-scheme split (one chunk per question) was drafted but never committed, so `/v1/ingest` keeps no `layout` parameter: every PDF, a legacy scheme that is reindexed included, is chunked as teaching material.

**Marking points stay, written by the teacher or AI.** A short answer or free response still takes up to five `marking_scheme` passages (20 per assessment) and `assessment.submitted.v3` still carries them to Grading's marker, unchanged on the wire. In the editor they are "Marking points": notes the teacher writes, or that drafted guidance and kept generated questions add. There is no search or matching of uploaded schemes. Passages saved before keep any `document_id`, `chunk_id` and `page` they had. Drafted guidance and generation read teaching materials only; the question-generation agent searches Content with `doc_types: ["teaching_material"]`, so a legacy scheme's id cannot be drafted from.

**Teachers can delete a document.** `DELETE /v1/documents/{id}` (the classroom's teacher only, 204) removes its original and figure crops from object storage, then deletes the row, which takes its chunks, vectors and indexing job with it, and writes `content.deleted.v1` (`{document_id, classroom_id, org_id}`) to the outbox in the same transaction. A failure to remove the objects leaves the row for a retry; a document already gone is 404. An indexing run still under way loses its job and removes any figure crops it stored afterwards, as it does after a classroom purge. The Materials page has a Delete button per document, behind the app's confirmation dialog with Cancel focused. Questions keep the source passages and marking points they copied; past chat answers keep their citation text, but the document's original can no longer be opened from them. `content.deleted.v1` has no consumer yet. (Superseded for chat by ADR 0010: the chatbot consumes `content.deleted.v1` and blanks the deleted document's citations.)

## Alternatives not taken

- **Keep scheme uploads and fix matching.** Every fix so far (chunking by question, taking the neighbouring table) still left the teacher checking each matched passage, which is the work matching was meant to save.
- **Drop marking points too.** Generated short answers would lose the notes that tell AI marking what earns each mark, and the event, Grading and the stored questions would all change for no gain.
- **Convert legacy schemes to teaching material.** Students would then see answer keys; they stay teacher-only until the teacher deletes them.

## Consequences

- `kafka-init` creates `content.deleted.v1`. An existing local setup needs only `docker compose up -d --build`; no migration and no `.env` change.
- The retrieval eval drops the rubric and marking scheme from its corpus and the teacher cases that expected them; the student leak checks that name them stay.
