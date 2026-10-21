-- no foreign key: a cleanup job must survive deletion of its document.
CREATE TABLE object_cleanup_jobs (
  prefix text PRIMARY KEY,
  attempts integer NOT NULL DEFAULT 0,
  next_attempt timestamptz NOT NULL DEFAULT now(),
  lease uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX object_cleanup_due_idx ON object_cleanup_jobs(next_attempt);
