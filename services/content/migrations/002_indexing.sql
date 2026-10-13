-- PDF indexing and search (docs/decisions/0002-ai-integration.md). Additive: existing rows keep their status,
-- and an earlier PDF ('extraction_unsupported') is indexed when a teacher asks for a reindex.
ALTER TABLE documents DROP CONSTRAINT documents_status_check;
ALTER TABLE documents ADD CONSTRAINT documents_status_check CHECK (status IN ('extracted','extraction_unsupported','indexing','indexed','failed'));
ALTER TABLE documents
  ADD COLUMN doc_type text NOT NULL DEFAULT 'teaching_material' CHECK (doc_type IN ('teaching_material','rubric','marking_scheme')),
  ADD COLUMN page_count integer CHECK (page_count >= 0),
  ADD COLUMN chunk_count integer CHECK (chunk_count >= 0),
  ADD COLUMN embedding_model text,
  ADD COLUMN indexed_at timestamptz,
  ADD COLUMN failure_reason text CHECK (failure_reason IN ('NO_TEXT_LAYER','ENCRYPTED','CORRUPT','AI_UNAVAILABLE','INTERNAL')),
  ADD COLUMN failure_detail text,
  ADD CONSTRAINT documents_failure_check CHECK ((status = 'failed') = (failure_reason IS NOT NULL)),
  ADD CONSTRAINT documents_indexed_check CHECK (status <> 'indexed' OR (page_count IS NOT NULL AND chunk_count IS NOT NULL AND embedding_model IS NOT NULL AND indexed_at IS NOT NULL)),
  -- the target of document_chunks' foreign key, which keeps each chunk's copy of the access columns identical
  ADD CONSTRAINT documents_access_key UNIQUE (id, org_id, classroom_id, doc_type);
CREATE INDEX documents_classroom_idx ON documents (org_id, classroom_id, created_at DESC);

-- Searchable pieces of indexed PDFs, written by the indexing worker from ai-core's /v1/ingest result.
CREATE TABLE document_chunks (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id     uuid        NOT NULL,
  org_id          text        NOT NULL,
  classroom_id    uuid        NOT NULL,
  doc_type        text        NOT NULL,
  ordinal         integer     NOT NULL CHECK (ordinal >= 0),
  section_path    text,                                         -- "Chapter 3 > Photosynthesis"
  page_start      integer     NOT NULL CHECK (page_start >= 1),
  page_end        integer     NOT NULL,
  content         text        NOT NULL CHECK (content <> ''),   -- shown and cited (a table: caption + Markdown)
  embed_text      text        NOT NULL,                         -- what the vector and the keyword index are built from
  token_count     integer     NOT NULL CHECK (token_count >= 0),
  kind            text        NOT NULL CHECK (kind IN ('text','table','figure')),
  source          text        NOT NULL CHECK (source IN ('pdf','vision')),  -- vision: written by a model
  image           text        CHECK (image ~ '^[a-f0-9]{16}\.png$'),        -- a figure's crop in object storage
  label           text,                                         -- a table's or figure's caption label: 'figure 7'
  cites           text[]      NOT NULL DEFAULT '{}',            -- labels a text chunk mentions
  embedding_model text        NOT NULL,
  embedding       vector(1536) NOT NULL,
  tsv             tsvector    GENERATED ALWAYS AS (to_tsvector('english'::regconfig, embed_text)) STORED,
  CHECK (page_end >= page_start),
  UNIQUE (document_id, ordinal),
  FOREIGN KEY (document_id, org_id, classroom_id, doc_type) REFERENCES documents (id, org_id, classroom_id, doc_type) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX document_chunks_tsv_gin ON document_chunks USING gin (tsv);
CREATE INDEX document_chunks_classroom_idx ON document_chunks (org_id, classroom_id);
CREATE INDEX document_chunks_label_idx ON document_chunks (document_id, label) WHERE label IS NOT NULL;

-- One pending indexing job per document. A worker leases a job by setting `lease` and pushing `next_attempt`
-- past the longest ingest, so a crashed worker's job is picked up again; only the lease holder may finish it.
CREATE TABLE index_jobs (
  document_id  uuid        PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
  attempts     integer     NOT NULL DEFAULT 0,
  next_attempt timestamptz NOT NULL DEFAULT now(),
  lease        uuid,
  last_error   text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX index_jobs_due_idx ON index_jobs (next_attempt);
