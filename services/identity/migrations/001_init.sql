CREATE TABLE IF NOT EXISTS organizations(id text PRIMARY KEY);
CREATE TABLE IF NOT EXISTS users(id text PRIMARY KEY, org_id text NOT NULL REFERENCES organizations(id), role text NOT NULL CHECK(role IN ('teacher','student')));
CREATE TABLE IF NOT EXISTS classrooms(id uuid PRIMARY KEY,name text NOT NULL CHECK(length(name) BETWEEN 1 AND 120),org_id text NOT NULL REFERENCES organizations(id),teacher_id text NOT NULL REFERENCES users(id),created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS enrollments(classroom_id uuid NOT NULL REFERENCES classrooms(id),student_id text NOT NULL REFERENCES users(id),PRIMARY KEY(classroom_id,student_id));
CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;
