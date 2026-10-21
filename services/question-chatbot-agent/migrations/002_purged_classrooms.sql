-- classrooms whose threads were purged when identity deleted them (identity.classroom.deleted.v1).
CREATE TABLE IF NOT EXISTS purged_classrooms(classroom_id uuid PRIMARY KEY, org_id text NOT NULL, purged_at timestamptz NOT NULL DEFAULT now());
