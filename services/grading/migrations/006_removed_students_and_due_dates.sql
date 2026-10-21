-- students their teacher removed from a classroom (identity.enrollment.removed.v1).
CREATE TABLE IF NOT EXISTS removed_enrollments(classroom_id uuid NOT NULL, student_id text NOT NULL, org_id text NOT NULL, removed_at timestamptz NOT NULL, PRIMARY KEY(classroom_id, student_id));
-- an assessment's due date as last changed after publishing (assessment.due_changed.v1).
CREATE TABLE IF NOT EXISTS assessment_due(assessment_id uuid PRIMARY KEY, classroom_id uuid NOT NULL, org_id text NOT NULL, due_at timestamptz, changed_at timestamptz NOT NULL);
