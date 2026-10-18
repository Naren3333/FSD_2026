-- The curriculum has one level, topics: each stored question names its topic in topic_id instead of skill_id.
-- Only the key is renamed, keeping its value, every other field and the questions' order; a question that already
-- has a topic_id keeps it. Saved answers and submissions hold answers by question id only, so they are unchanged.
-- Rows already in the outbox keep the event they were written as.
UPDATE assessments a
SET questions = (
    SELECT coalesce(jsonb_agg(
        CASE WHEN jsonb_typeof(q) = 'object' AND q ? 'skill_id'
            THEN (q - 'skill_id') || jsonb_build_object('topic_id', coalesce(q->'topic_id', q->'skill_id'))
            ELSE q
        END ORDER BY n), '[]'::jsonb)
    FROM jsonb_array_elements(a.questions) WITH ORDINALITY AS e(q, n)
)
WHERE jsonb_typeof(a.questions) = 'array'
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(a.questions) q WHERE jsonb_typeof(q) = 'object' AND q ? 'skill_id');
