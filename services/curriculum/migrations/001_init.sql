CREATE TABLE IF NOT EXISTS nodes(id uuid PRIMARY KEY,org_id text NOT NULL,kind text NOT NULL CHECK(kind IN ('subject','course','unit','topic','skill','objective')),name text NOT NULL CHECK(length(name) BETWEEN 1 AND 160),parent_id uuid REFERENCES nodes(id),archived boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS prerequisites(skill_id uuid NOT NULL REFERENCES nodes(id),prerequisite_id uuid NOT NULL REFERENCES nodes(id),PRIMARY KEY(skill_id,prerequisite_id),CHECK(skill_id<>prerequisite_id));
CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;
