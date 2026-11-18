-- migrate: concurrent-index
CREATE INDEX CONCURRENTLY usage_by_date ON usage (created_at);
