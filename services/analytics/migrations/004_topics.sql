-- the curriculum has one level, the topic: evidence names its topic as topic_id.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'evidence' AND column_name = 'skill_id') THEN
    ALTER TABLE evidence RENAME COLUMN skill_id TO topic_id;
  END IF;
END $$;
