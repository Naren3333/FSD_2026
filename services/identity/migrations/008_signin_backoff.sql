-- back off failed account removals so they cannot monopolize deletion batches.
ALTER TABLE users ADD COLUMN IF NOT EXISTS signin_attempts int NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS signin_next_attempt timestamptz;
CREATE INDEX IF NOT EXISTS users_signin_due ON users(signin_next_attempt NULLS FIRST, deleted_at) WHERE deleted_at IS NOT NULL AND signin_removed_at IS NULL;
