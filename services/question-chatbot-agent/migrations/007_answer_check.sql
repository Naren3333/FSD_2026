-- the support check of an answer (answer_check.v1, adr 0025): its status (checked, uncited, failed) and, when
-- checked, each cited statement's verdict and the counts. null for refusals, small talk and earlier answers.
-- additive only.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS answer_check jsonb;
