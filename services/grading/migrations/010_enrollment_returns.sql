-- when each student was last enrolled in a classroom (identity.enrollment.added.v1), so work submitted before
-- an older removal is marked again once they are back. additive only.
CREATE TABLE IF NOT EXISTS enrollment_returns(classroom_id uuid NOT NULL, student_id text NOT NULL, org_id text NOT NULL, restored_at timestamptz NOT NULL, PRIMARY KEY(classroom_id, student_id));
-- an account's purge finds its removals and returns. a return finds the student's drafts through an index built
-- beside grading's work instead (indexes), so the build never holds the writes to evaluations.
CREATE INDEX IF NOT EXISTS removed_enrollments_student ON removed_enrollments(student_id);
CREATE INDEX IF NOT EXISTS enrollment_returns_student ON enrollment_returns(student_id);
