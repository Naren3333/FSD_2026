-- migrate: concurrent-index
CREATE INDEX CONCURRENTLY evaluations_classroom ON evaluations (classroom_id);
