-- Students who deleted their account (identity.user.deleted.v1, ADR 0011). Their evidence is purged, and a finalized
-- grade of theirs that arrives after the purge is ignored, so no evidence comes back. Additive only.
CREATE TABLE IF NOT EXISTS deleted_users(user_id text PRIMARY KEY, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
