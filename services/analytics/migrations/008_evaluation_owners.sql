-- each evaluation's assessment, classroom and student, so a purge also removes one withdrawn for correction,
-- which keeps no evidence. additive, and committed on its own (open_migrated_pool), so its table lock is held
-- only for these catalog changes, not through the backfill and the index builds after it.
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS assessment_id uuid;
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS classroom_id uuid;
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS org_id text;
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS student_id text;
-- from now on every new evaluation names its owners: a replica still running older code during a rolling
-- upgrade would store one without them, which no purge could find. its write is refused with an error the
-- consumer retries (not a constraint violation, which it would dead-letter), so the event waits for a replica
-- that names them. rows stored before keep theirs until the backfill or their next revision.
CREATE OR REPLACE FUNCTION evaluations_need_owners() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'an evaluation must name its assessment, classroom, organization and student (migration 008)'
    USING ERRCODE = 'object_not_in_prerequisite_state';
END $$;
CREATE OR REPLACE TRIGGER evaluations_owned BEFORE INSERT ON evaluations FOR EACH ROW
  WHEN (NEW.assessment_id IS NULL OR NEW.classroom_id IS NULL OR NEW.org_id IS NULL OR NEW.student_id IS NULL)
  EXECUTE FUNCTION evaluations_need_owners();
