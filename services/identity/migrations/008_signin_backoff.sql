-- A sign-in removal that fails is retried with a growing delay, so accounts Keycloak keeps refusing to remove do not
-- take every batch and hold back the rest: signin_attempts counts the failures and signin_next_attempt is when the
-- next one is due (NULL: at once). Additive only.
ALTER TABLE users ADD COLUMN IF NOT EXISTS signin_attempts int NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS signin_next_attempt timestamptz;
CREATE INDEX IF NOT EXISTS users_signin_due ON users(signin_next_attempt NULLS FIRST, deleted_at) WHERE deleted_at IS NOT NULL AND signin_removed_at IS NULL;
