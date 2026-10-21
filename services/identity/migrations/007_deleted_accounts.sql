-- a user can delete their own account (adr 0011).
ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS signin_removed_at timestamptz;
CREATE INDEX IF NOT EXISTS users_signin_pending ON users(deleted_at) WHERE deleted_at IS NOT NULL AND signin_removed_at IS NULL;
