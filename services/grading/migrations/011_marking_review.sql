-- the marking critic (adr 0020): a job first suggests a mark, then a second, blind marking checks a ready one.
-- additive only: existing jobs stay suggestion jobs.
ALTER TABLE marking_jobs ADD COLUMN IF NOT EXISTS stage text NOT NULL DEFAULT 'suggest' CHECK (stage IN ('suggest','review'));
