-- published assessments their teacher deleted with their submissions (assessment.deleted.v1, adr 0008).
CREATE TABLE IF NOT EXISTS deleted_assessments(assessment_id uuid PRIMARY KEY, classroom_id uuid NOT NULL, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
