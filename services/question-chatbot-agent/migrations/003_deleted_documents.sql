-- documents their teacher deleted (content.deleted.v1, adr 0009).
CREATE TABLE IF NOT EXISTS deleted_documents(document_id uuid PRIMARY KEY, classroom_id uuid NOT NULL, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
