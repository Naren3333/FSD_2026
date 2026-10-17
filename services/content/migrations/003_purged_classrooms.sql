-- Classrooms whose documents were purged when Identity deleted them (identity.classroom.deleted.v1). An upload that
-- was already under way when the purge ran is refused instead of storing a document in the deleted classroom.
-- Additive only.
CREATE TABLE IF NOT EXISTS purged_classrooms(classroom_id uuid PRIMARY KEY, org_id text NOT NULL, purged_at timestamptz NOT NULL DEFAULT now());
