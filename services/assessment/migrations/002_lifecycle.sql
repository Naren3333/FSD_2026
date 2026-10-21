-- drafts, scheduling, due dates, release and ai-marking settings.
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'published' CHECK(status IN ('draft','published'));
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS opens_at timestamptz;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS due_at timestamptz;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS late_policy text NOT NULL DEFAULT 'accept_flagged' CHECK(late_policy IN ('accept_flagged','reject'));
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS release_policy text NOT NULL DEFAULT 'on_finalize' CHECK(release_policy IN ('on_finalize','manual'));
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS ai_marking boolean NOT NULL DEFAULT false;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS published_at timestamptz;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS created_by text;
UPDATE assessments SET published_at=created_at WHERE status='published' AND published_at IS NULL;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS late boolean NOT NULL DEFAULT false;
-- a student's unsubmitted answers, saved as they work.
CREATE TABLE IF NOT EXISTS saved_answers(assessment_id uuid NOT NULL REFERENCES assessments(id), student_id text NOT NULL, answers jsonb NOT NULL, saved_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(assessment_id, student_id));
