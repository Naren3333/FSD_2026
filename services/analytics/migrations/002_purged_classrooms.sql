-- Classrooms whose evidence was purged when Identity deleted them (identity.classroom.deleted.v1). A finalized grade
-- for one of them that arrives after the purge is ignored, so no evidence comes back. Additive only.
CREATE TABLE IF NOT EXISTS purged_classrooms(classroom_id uuid PRIMARY KEY, org_id text NOT NULL, purged_at timestamptz NOT NULL DEFAULT now());
