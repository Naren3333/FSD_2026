-- school admins manage their school's accounts and classrooms (adr 0019).
-- widens the role check only; no row changes.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK(role IN ('teacher','student','admin'));
CREATE INDEX IF NOT EXISTS classrooms_org_created ON classrooms(org_id, created_at DESC, id DESC) WHERE deleted_at IS NULL;
