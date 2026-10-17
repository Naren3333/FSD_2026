-- Classrooms whose threads were purged when Identity deleted them (identity.classroom.deleted.v1). A thread is never
-- created in one of them afterwards, even by a request Identity allowed just before the deletion. Additive only.
CREATE TABLE IF NOT EXISTS purged_classrooms(classroom_id uuid PRIMARY KEY, org_id text NOT NULL, purged_at timestamptz NOT NULL DEFAULT now());
