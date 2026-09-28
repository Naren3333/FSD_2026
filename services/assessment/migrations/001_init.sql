CREATE TABLE IF NOT EXISTS assessments(id uuid PRIMARY KEY,classroom_id uuid NOT NULL,org_id text NOT NULL,title text NOT NULL,questions jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS submissions(id uuid PRIMARY KEY,assessment_id uuid NOT NULL REFERENCES assessments(id),student_id text NOT NULL,answers jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(assessment_id,student_id));
CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;
