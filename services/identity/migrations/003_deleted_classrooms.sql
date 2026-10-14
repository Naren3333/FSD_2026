-- A teacher can delete a classroom. Its row, enrollments and every other service's work for it are kept; a deleted
-- classroom is hidden from lists and refuses all access, so nothing reaches its data. Additive only.
ALTER TABLE classrooms ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
