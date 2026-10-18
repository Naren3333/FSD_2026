-- Published assessments their teacher deleted with their submissions (assessment.deleted.v1, ADR 0008). Their
-- evidence is purged, and a finalized grade for one that arrives after the purge is ignored, so no evidence comes
-- back. Additive only.
CREATE TABLE IF NOT EXISTS deleted_assessments(assessment_id uuid PRIMARY KEY, classroom_id uuid NOT NULL, org_id text NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now());
