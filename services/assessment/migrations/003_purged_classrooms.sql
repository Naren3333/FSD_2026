-- Classrooms whose assessments were purged when Identity deleted them (identity.classroom.deleted.v1). An assessment
-- created while the purge ran is refused, so none is left behind in a deleted classroom. Additive only.
CREATE TABLE IF NOT EXISTS purged_classrooms(classroom_id uuid PRIMARY KEY, org_id text NOT NULL, purged_at timestamptz NOT NULL DEFAULT now());
