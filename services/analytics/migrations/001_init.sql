CREATE TABLE IF NOT EXISTS inbox(event_id uuid PRIMARY KEY,processed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS evaluations(evaluation_id uuid PRIMARY KEY,event_id uuid UNIQUE NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS evidence(evaluation_id uuid NOT NULL REFERENCES evaluations(evaluation_id),question_id uuid NOT NULL,assessment_id uuid NOT NULL,classroom_id uuid NOT NULL,org_id text NOT NULL,student_id text NOT NULL,skill_id uuid NOT NULL,correct boolean NOT NULL,awarded integer NOT NULL CHECK(awarded BETWEEN 0 AND 1),possible integer NOT NULL CHECK(possible=1),PRIMARY KEY(evaluation_id,question_id));
CREATE INDEX IF NOT EXISTS evidence_lookup ON evidence(org_id,classroom_id,student_id);
