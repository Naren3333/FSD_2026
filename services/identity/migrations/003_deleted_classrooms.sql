-- a teacher can delete a classroom.
ALTER TABLE classrooms ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
