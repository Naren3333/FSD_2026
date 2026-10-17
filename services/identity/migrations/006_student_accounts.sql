-- Accounts hold exactly one role, teacher or student, and keep it. Enrollment and the organization's student search
-- look students up by role, which this index serves. Additive only.
CREATE INDEX IF NOT EXISTS users_org_role ON users(org_id, role);
