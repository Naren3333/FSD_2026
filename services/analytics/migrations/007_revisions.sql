-- the newest revision of each evaluation stored, and whether its teacher reopened it since (adr 0016).
-- additive only: every stored evaluation is revision 1 and not withdrawn.
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1;
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS withdrawn boolean NOT NULL DEFAULT false;
