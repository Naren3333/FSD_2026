-- deleting a classroom now unenrolls its students.
DELETE FROM enrollments e USING classrooms c WHERE c.id = e.classroom_id AND c.deleted_at IS NOT NULL;
