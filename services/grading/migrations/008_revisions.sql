-- a grade's revision goes up each time its teacher reopens it (adr 0016); every grade before is revision 1.
-- additive only.
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1;
