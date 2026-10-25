-- a teacher can delete a classroom.
-- additive only.
ALTER TABLE classrooms ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
