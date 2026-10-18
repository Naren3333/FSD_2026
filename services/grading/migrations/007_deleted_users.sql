-- Users who deleted their account (ADR 0011): their evaluations were purged, and a submission event that arrives
-- later grades nothing. Additive only.
CREATE TABLE IF NOT EXISTS deleted_users(user_id text PRIMARY KEY, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
