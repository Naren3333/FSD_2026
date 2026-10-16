-- grading.finalized.v2: a question is worth 1–100 points and may earn part of them (ADR 0004). Additive: the checks
-- only widen, so every stored row still satisfies them.
ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_awarded_check;
ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_possible_check;
ALTER TABLE evidence ADD CONSTRAINT evidence_possible_check CHECK(possible BETWEEN 1 AND 100);
ALTER TABLE evidence ADD CONSTRAINT evidence_awarded_check CHECK(awarded BETWEEN 0 AND possible);
