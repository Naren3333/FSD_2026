-- Users who deleted their account (identity.user.deleted.v1, ADR 0011): their threads and messages were removed,
-- and no thread is created for them afterwards. Additive only.
CREATE TABLE IF NOT EXISTS deleted_users(user_id text PRIMARY KEY, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
