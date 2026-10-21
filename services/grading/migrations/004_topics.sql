-- the curriculum has one level, the topic: every stored question and evidence item calls its curriculum
-- reference topic_id instead of skill_id.
CREATE FUNCTION pg_temp.topic_ids(items jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN jsonb_typeof(items) <> 'array' OR NOT jsonb_path_exists(items, '$[*].skill_id') THEN items ELSE (
    SELECT jsonb_agg(CASE
      WHEN jsonb_typeof(item) <> 'object' OR NOT item ? 'skill_id' THEN item
      WHEN item ? 'topic_id' THEN item - 'skill_id'
      ELSE (item - 'skill_id') || jsonb_build_object('topic_id', item -> 'skill_id') END ORDER BY n)
    FROM jsonb_array_elements(items) WITH ORDINALITY AS t(item, n)) END
$$;
UPDATE evaluations SET evidence = pg_temp.topic_ids(evidence) WHERE jsonb_path_exists(evidence, '$[*].skill_id');
UPDATE evaluations SET questions = pg_temp.topic_ids(questions) WHERE jsonb_path_exists(questions, '$[*].skill_id');
UPDATE audit SET before_evidence = pg_temp.topic_ids(before_evidence), after_evidence = pg_temp.topic_ids(after_evidence)
  WHERE jsonb_path_exists(before_evidence, '$[*].skill_id') OR jsonb_path_exists(after_evidence, '$[*].skill_id');
DROP FUNCTION pg_temp.topic_ids(jsonb);
