-- the curriculum has one level, the topic: evidence names its topic as topic_id.
-- the rename keeps every stored row and runs only while the old column exists, so it is safe to run again.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'evidence' AND column_name = 'skill_id') THEN
    ALTER TABLE evidence RENAME COLUMN skill_id TO topic_id;
  END IF;
END $$;
