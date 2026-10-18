-- A user can delete their own account (ADR 0011). The row is kept as the record of the deletion, since deleted
-- classrooms still name their teacher, but its names are cleared; signin_removed_at is when Identity removed the
-- sign-in account from Keycloak. Additive only.
ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS signin_removed_at timestamptz;
CREATE INDEX IF NOT EXISTS users_signin_pending ON users(deleted_at) WHERE deleted_at IS NOT NULL AND signin_removed_at IS NULL;
