-- Documents their teacher deleted (content.deleted.v1, ADR 0009). The passages past answers copied from one are
-- blanked and marked removed, and an answer saved after the deletion is stored the same way. Additive only.
CREATE TABLE IF NOT EXISTS deleted_documents(document_id uuid PRIMARY KEY, classroom_id uuid NOT NULL, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
