-- Deleting a classroom now unenrolls its students. This removes the enrollments that classrooms deleted before
-- then kept, so no student stays tied to a deleted classroom. Authorized by the project owner on 2026-10-14.
DELETE FROM enrollments e USING classrooms c WHERE c.id = e.classroom_id AND c.deleted_at IS NOT NULL;
