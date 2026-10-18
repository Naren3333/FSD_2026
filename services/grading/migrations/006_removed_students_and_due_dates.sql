-- Students their teacher removed from a classroom (identity.enrollment.removed.v1). Their evaluations are kept, but
-- an AI suggestion for work submitted before the removal is no longer made: it stops with STUDENT_REMOVED. A student
-- enrolled again keeps the latest removal, so only work submitted after it is marked. Additive only.
CREATE TABLE IF NOT EXISTS removed_enrollments(classroom_id uuid NOT NULL, student_id text NOT NULL, org_id text NOT NULL, removed_at timestamptz NOT NULL, PRIMARY KEY(classroom_id, student_id));
-- An assessment's due date as last changed after publishing (assessment.due_changed.v1). Each evaluation's late
-- flag follows it, including one whose submission event arrives after the change. Additive only.
CREATE TABLE IF NOT EXISTS assessment_due(assessment_id uuid PRIMARY KEY, classroom_id uuid NOT NULL, org_id text NOT NULL, due_at timestamptz, changed_at timestamptz NOT NULL);
