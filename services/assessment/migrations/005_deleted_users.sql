-- Users who deleted their account (ADR 0011): their submissions and saved work were purged, and a submission or
-- save racing the deletion is refused. Additive only.
CREATE TABLE IF NOT EXISTS deleted_users(user_id text PRIMARY KEY, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
