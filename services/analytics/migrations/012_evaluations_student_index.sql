-- migrate: concurrent-index
CREATE INDEX CONCURRENTLY evaluations_student ON evaluations (student_id);
