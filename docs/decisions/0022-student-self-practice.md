# ADR 0022: Student self-practice

Status: accepted 2026-11-22. The project owner asked for students to practise from their class's materials (requirements Feature 4 and Service 8), which ADR 0006 and ADR 0017 left for their own ADR. It amends ADR 0006: the question-generation agent gains a second audience, and it stays one of the ten applications (ADR 0021 precedent).

## Context

Only a classroom's teacher can call the question-generation agent, and the student's My progress page says generated practice is not available yet. A student who wants to check what they know from the slides has nothing but the chat. Generated practice has no teacher in the loop, so it cannot rely on the teacher reviewing each question as ADR 0006 does, and it must not touch grades or evidence.

## Decision

**Practice is a third route in `question-generation-agent`, on the same pipeline.**

- `POST /v1/practice` takes a classroom, 1–10 of its documents (whole documents, no page ranges), an optional curriculum topic, an optional focus (up to 300 characters), a count for multiple choice and short answer (1–10 each, at most 10 in total), a difficulty, and up to 50 prompts the student already has, so "More questions" does not repeat them. Free response is refused (422): it needs a rubric and a marker.
- Only a student enrolled in the classroom may call it (the token's student role and Identity's `student` relation). Teachers, students of other classrooms and service accounts get 403. It is published through the existing Kong route `/api/question-generation` and called with the student's own token, so Content's search shows them only the classroom's teaching material. Kong, Keycloak and Compose need no change.
- The search query is the topic's name and the focus when a topic is chosen (an unknown topic is `422 UNKNOWN_TOPIC`), the focus alone without one, and the chosen documents' titles, read from Content's document list with the student's token, when the student gives neither.
- The relevance floor, the rerank rule, the checks and the critic loop are those of ADR 0006 and ADR 0015. Every model call is one structured ai-core `generate` call as the `question-generation-agent` service account, billed to the student's own allowance (ADR 0003), under the schema names `practice.generated` and `practice.reviewed` so the ledger tells them apart from a teacher's. The instructions address a student practising alone: each question answerable from its passages, and an explanation written to the student that points to the passage.

**No teacher reviews practice, so review fails closed.**

- The reviewer pass always runs; a student cannot skip it. When the service's review is switched off (`QUESTION_REVIEW`), practice answers `503 AI_UNAVAILABLE` rather than return unreviewed questions.
- Only drafts the reviewer passed are returned. Drafts it flagged (weak distractors, a long key) and drafts it gave no verdict on, or could not review, are dropped and counted in `dropped`. When nothing passes, the request fails with `422 GENERATION_REJECTED` and practice wording, or with the upstream error that stopped the retries, as for a teacher. When nothing passed because the reviewer never judged the drafts (cut off at the deadline, an unreadable reply or no verdict), it fails with `503 REVIEW_UNAVAILABLE` and asks the student to try again: nothing was refused, so choosing other materials would not help.

**Nothing is saved, and nothing is graded.**

- The set lives in the browser tab. There is no grade, submission, evidence or event, so Grading, Analytics and the "Needs practice" flag are unchanged.
- Each question comes back with its key (the option, or the accepted answers), an explanation, the quoted evidence and its source passages. These keys are AI-written for the student's own ungraded use; this is the one exception to "never expose answer keys to students". Assessment's keys stay hidden exactly as before. The page reveals each key only after the student answers that question; a student could read the response in the browser's tools, which is accepted for ungraded practice.
- Answers are checked in the browser: the chosen option against the key, and a short answer against the accepted answers after folding case, whitespace and punctuation. A short answer that does not match shows the accepted answers to compare with, since a near-miss may still be right.

**The page** is a student-only "Practice" section under Learn. It lists the classroom's indexed teaching materials to tick, a topic select with "Any topic", a focus, counts, a difficulty, and says each set uses the daily AI allowance. Each question carries "AI-written practice". The student answers, checks, and then sees whether they were right, the explanation and the passages, with a running score and "More questions".

## Consequences

- A set costs up to six model calls (three writes, three reviews), counted against the student's $5 daily allowance.
- Without provider keys, practice answers `503 SEARCH_UNAVAILABLE` or `AI_PROVIDER_NOT_CONFIGURED`, and the page shows the message.
- Fail-closed review returns fewer questions than a teacher's request would; a student may see "None of the practice questions passed the checks".
- `scripts/eval/run_generation.py --practice` runs the generation cases as their classroom's enrolled student, with multiple choice and short answer only.
- Not built: saving sets or answers, free-response practice, page or section ranges, practice targeted at a student's weak topics from Analytics (ADR 0014's evidence), and teachers seeing what their students practised.
