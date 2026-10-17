-- An account may hold both the teacher and the student role. users.role keeps the role an account acts in by
-- default (teacher when it holds both); student records whether it may be enrolled, so a dual-role account can join
-- another teacher's classroom. Accounts registered as students get it now; a dual-role account registered before this
-- migration gets it the next time it signs in. Additive only.
ALTER TABLE users ADD COLUMN IF NOT EXISTS student boolean NOT NULL DEFAULT false;
UPDATE users SET student=true WHERE role='student' AND NOT student;
CREATE INDEX IF NOT EXISTS users_org_students ON users(org_id) WHERE student;
