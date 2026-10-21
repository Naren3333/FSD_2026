-- the curriculum becomes one level, topics (adr 0007, decided by the project owner on 2026-10-18).
DELETE FROM prerequisites
WHERE skill_id IN (SELECT id FROM nodes WHERE kind <> 'skill')
   OR prerequisite_id IN (SELECT id FROM nodes WHERE kind <> 'skill');
UPDATE nodes SET parent_id = NULL WHERE parent_id IS NOT NULL;
DELETE FROM nodes WHERE kind <> 'skill';
ALTER TABLE nodes DROP COLUMN parent_id;
ALTER TABLE nodes DROP COLUMN kind;
ALTER TABLE nodes RENAME TO topics;
ALTER TABLE prerequisites RENAME COLUMN skill_id TO topic_id;
ALTER INDEX IF EXISTS nodes_pkey RENAME TO topics_pkey;
ALTER TABLE topics RENAME CONSTRAINT nodes_name_check TO topics_name_check;
ALTER TABLE prerequisites RENAME CONSTRAINT prerequisites_skill_id_fkey TO prerequisites_topic_id_fkey;
