-- points, teacher marking, release and ai-suggested marking.
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS questions jsonb;
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS answers jsonb;
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS late boolean NOT NULL DEFAULT false;
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS submitted_at timestamptz;
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS release_policy text NOT NULL DEFAULT 'on_finalize' CHECK(release_policy IN ('on_finalize','manual'));
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS released_at timestamptz;
-- the teacher whose daily ai allowance pays for the evaluation's ai suggestions (adr 0003), when it has
-- any.
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS teacher_id text;
UPDATE evaluations SET released_at=finalized_at WHERE status='finalized' AND released_at IS NULL;
CREATE INDEX IF NOT EXISTS evaluations_assessment ON evaluations(org_id, assessment_id);
-- one ai suggestion to make per question that needs the teacher, leased by a marking worker.
CREATE TABLE IF NOT EXISTS marking_jobs(
  evaluation_id uuid NOT NULL REFERENCES evaluations(id),
  question_id uuid NOT NULL,
  attempts integer NOT NULL DEFAULT 0,
  run_after timestamptz NOT NULL DEFAULT now(),
  leased_until timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(evaluation_id, question_id)
);
CREATE INDEX IF NOT EXISTS marking_jobs_due ON marking_jobs(run_after);
