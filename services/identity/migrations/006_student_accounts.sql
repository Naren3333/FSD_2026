-- accounts hold exactly one role, teacher or student, and keep it.
CREATE INDEX IF NOT EXISTS users_org_role ON users(org_id, role);
