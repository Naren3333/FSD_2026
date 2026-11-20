-- migrate: concurrent-index
CREATE INDEX CONCURRENTLY evaluations_assessment ON evaluations (assessment_id);
