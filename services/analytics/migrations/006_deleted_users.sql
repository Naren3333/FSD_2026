-- students who deleted their account (identity.user.deleted.v1, adr 0011).
CREATE TABLE IF NOT EXISTS deleted_users(user_id text PRIMARY KEY, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
