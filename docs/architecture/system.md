# Architecture actually implemented

```mermaid
flowchart LR
  Browser[Angular browser] -->|OIDC code + PKCE| Keycloak
  Browser -->|Bearer token| Kong[Kong OSS]
  Kong --> Identity[Identity / Go]
  Kong --> Curriculum[Curriculum / Go]
  Kong --> Content[Content / Go]
  Kong --> Assessment[Assessment / Go]
  Kong --> Grading[Grading / Go]
  Kong --> Analytics[Analytics / Python]
  Kong --> QC
  QC -->|search with the user's token| Content
  QC -->|rewrite, answer| AI
  QC --> ChatDB[(chatbot database)]
  Content --> S3[Content-only S3 bucket]
  Content -->|authorize| Identity
  Assessment -->|authorize| Identity
  Assessment -->|validate skill| Curriculum
  Grading -->|authorize| Identity
  Analytics -->|authorize| Identity
  Assessment -->|outbox: submitted| Kafka
  Kafka --> Grading
  Grading -->|outbox: finalized, at release| Kafka
  Kafka --> Analytics
  Content -->|outbox: uploaded, indexed| Kafka
  Identity -->|outbox: classroom deleted| Kafka
  Kafka -->|purge deleted classroom| Content
  Kafka -->|purge deleted classroom| Assessment
  Kafka -->|purge deleted classroom| Grading
  Kafka -->|purge deleted classroom| Analytics
  Kafka -->|purge deleted classroom| QC
  AI --> AIDB[(aicore usage ledger)]
  Content -->|ingest, embed, rerank as content-indexer| AI
  Grading -->|marking suggestions as grading-marker| AI
  subgraph AI applications
    AI[AI core: ingest, embed, generate, rerank]
    QG[Question generation: 501]
    SP[Performance interpretation: 501]
    QC[Question chatbot]
  end
```

Content and the chatbot call ai-core: its indexing worker sends PDFs to ai-core, and search asks ai-core for the query vector and the rerank (ADR 0002). ai-core calls nothing back, so there is no circular dependency. Each persistent application connects only to its own database. Database and schema grants enforce the boundary; identifiers across services are references, not cross-database foreign keys. Assessment validates skill references through Curriculum. Identity is the policy owner for classroom access; bearer tokens forwarded by a business service are revalidated at Identity. No user identity headers are trusted.

Go domain files implement exact grading, assessment validation and text chunking without framework/database dependencies, except the small transport UUID helper used by assessment validation (a remaining layering cleanup). SQL is explicit, application transactions are visible in handlers, and source remains intentionally small. The shared Go package implements transport, auth, connection lifecycle, outbox plumbing and consumer inboxes; it owns no educational entity. Python's shared package implements auth/error/logging, database pools and the Kafka consumer loop only.

## Data owners

Identity owns `organizations`, `users`, `classrooms`, `enrollments`. Curriculum owns `nodes`, `prerequisites`. Content owns `documents` including extracted text chunks and SHA-256/source revision metadata, `document_chunks` (indexed PDF chunks with pgvector embeddings and a full-text index), `index_jobs`, plus bucket objects (originals and figure crops). Assessment owns `assessments` with question snapshots, answer keys, rubrics and lifecycle settings, `submissions` and students' unsubmitted `saved_answers`. Grading owns `evaluations` (with the questions and answers they mark), `audit` and `marking_jobs`. Analytics owns its `evaluations`, `evidence` and `inbox` read model. The question chatbot owns `threads` and `messages` in its `chatbot` database. ai-core owns its `aicore` database: the `usage` ledger of every paid model call and its cost (ADR 0003), per user, with no classroom. Outbox/inbox tables are independently created in the application owner's database, not a shared event database.

When Identity deletes a classroom it emits `identity.classroom.deleted.v1`, and Content, Assessment, Grading, Analytics and the chatbot each purge their own data for it in their consumer's transaction. Grading, Analytics, Content and the chatbot also keep `purged_classrooms`, the deleted classrooms whose data they purged, so a late event cannot bring it back (Content also refuses an upload that was under way when the purge ran, and the chatbot a new thread). ai-core's usage records are not purged (billing, per user).

The first migration is non-destructive and idempotent. Go migrations are embedded from each service's `migrations/NNN_name.sql` files and applied in order under a PostgreSQL advisory transaction lock; each version runs once and is recorded in `schema_migrations` (versions must run 1..n without gaps, or startup fails). The Python services that own a database (Analytics, ai-core, the chatbot) apply theirs the same way through `learning_transport.db`, under the same lock id and with the same `schema_migrations` table. Schema changes go in a new numbered file, never an edit to an applied one; `CREATE TABLE IF NOT EXISTS` is not an upgrade mechanism. Production migrations should run with separate owner credentials; local app roles own only their own database.

## Security and state

Kong verifies RS256 and expiration using the provisioned issuer/public key. Backends also verify issuer/audience/expiration, organization and application role. Classroom teachers can act only on their own classrooms. Identity's access answer names the caller's relation to the classroom (`teacher` for its owner, `student` for an enrolled student), and teacher-only data (answer keys, rubrics, marking schemes, grade review) is gated on that relation, never on the token's `teacher` role. An account is a teacher or a student, never both: every backend refuses a token holding both roles (403 `ROLE_CONFLICT`), Identity keeps the role an account first registered with, and only student accounts can be enrolled. Students require enrollment and can query only their own evidence and their own finalized grades; draft grades are visible to the classroom's teacher only. Only a classroom's teacher can list its roster or remove a student. Students never receive answer keys from Assessment's public projection. The broker's assessment topic contains confidential answer-key snapshots and must receive strict production ACLs.

Assessments start as drafts the teacher can change or delete; once published they are immutable (ADR 0004). Students see a published assessment only after its `opens_at`; a submission after `due_at` is flagged late or refused, as its late policy says. Submission is unique by assessment/student. Short answers that match no accepted answer and rubric-marked free responses wait for the teacher (`needs_marking`), and finalization is refused until every question is marked. With a manual release policy, finalized grades reach students only when the teacher releases the assessment's results. Grading drafts are unique by submission and store reason plus before/after evidence in an audit table. Finalization locks the evaluation row and atomically writes the updated record and audit entry. `grading.finalized.v2` is written to the outbox, in the same transaction, when the grade is released: at finalization for an assessment that releases on finalization, and in the release for a manual one. Analytics therefore only ever holds released grades, so a student's own performance never shows a grade they cannot yet see. Retrying finalization receives 409. Analytics separately deduplicates event IDs and evaluation IDs so a new event ID for the same finalized grade cannot add evidence twice.

## AI and retrieval

```mermaid
flowchart LR
  Upload[Teacher uploads a PDF] --> Job[Content: document indexing + index job]
  Job --> Ingest[ai-core /v1/ingest: parse, chunk, embed]
  Ingest --> Vector[Content: document_chunks, pgvector + full text]
  Vector --> Event[content.indexed.v1]
  Search[POST /v1/search with the caller's token] --> Vector
  Search --> Rerank[ai-core embed + rerank]
  Student[Authenticated student question] --> Chat[Question chatbot agent]
  Chat -->|user's own token| Search
  Chat --> Inference[ai-core generation]
  Inference -->|streamed answer + citations| Student
```

ai-core implements `/v1/ingest` (PDF bytes in; chunks, vectors and figure crops out), `/v1/inference` (embed, generate, structured or streamed generation) and `/v1/rerank` (Content sends each hit's title, section path and content, and ai-core builds the scored text with the same rule it used for the embedded text), statelessly: Content stores the results. Provider failures are errors (502/503), never placeholder output; Content retries indexing and answers search with 503 `SEARCH_UNAVAILABLE` instead of inventing results.

Every paid call is charged to a user (ADR 0003): callers pass `billed_to` (the searching or chatting user, or the teacher who uploaded the document being indexed), and ai-core records each call's cost in its own `aicore` database. Before a call it refuses with `429 AI_SPEND_LIMIT` once the user's spend since local midnight reaches the daily limit ($5 for a student, $10 for a teacher). The chatbot shows the user their spend through `GET /v1/usage`.

Content's index: a PDF upload commits the document (status `indexing`) and its `index_jobs` row together. Worker goroutines lease due jobs with `FOR UPDATE SKIP LOCKED`; the result is stored in one transaction that first deletes the job by its lease, so a run that lost its lease (outlived it, or was taken over) changes nothing and `content.indexed.v1` is emitted once per completed run. Chunks carry copies of the document's org, classroom and type, kept identical by a composite foreign key, so search filters on them without a join; vectors are compared only within the query's embedding model. A reindex keeps the old chunks searchable until the new result replaces them; a reindex that fails leaves them searchable while the document shows `failed`. Cached query vectors expire after 10 minutes and are dropped when the embedding model changes. Search applies the caller's own classroom access (through Identity) and, for students, teaching material only; the chatbot calls it with the student's token, so an agent never gets wider access than its user. The chatbot stores only threads and messages (with the chunk ids each answer was given and cited) in its own `chatbot` database.

Grading asks ai-core for marking suggestions when an assessment turns them on (ADR 0004). Each question that needs the teacher gets a `marking_jobs` row with its draft; workers lease them like Content's index jobs and make one structured call each, billed to the assessment's teacher. Grading checks the reply against the rubric (known levels, every criterion once, quotes that appear in the answer) and stores it on the draft as a suggestion only. The teacher decides every mark, and students never see suggestions.

The other two private Python agents define versioned input/output contracts and return `AI_CAPABILITY_NOT_IMPLEMENTED` (501) after authentication, role checking and validation. None accesses business data or uses test doubles in runtime code. The agent contracts carry untrusted references; accepting a syntactically valid reference is not authorization to retrieve it.
