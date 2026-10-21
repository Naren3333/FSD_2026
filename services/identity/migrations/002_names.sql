-- names for classroom rosters, taken from the user's sign-in token and refreshed each time they register.
ALTER TABLE users ADD COLUMN IF NOT EXISTS username text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name text;
CREATE INDEX IF NOT EXISTS users_org_username ON users(org_id, lower(username));
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS enrolled_at timestamptz NOT NULL DEFAULT now();
