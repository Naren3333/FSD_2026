-- 008's refusal of evaluations without owners, for a database that applied 008 before it carried it, and the
-- owners of any evaluation with evidence stored without them meanwhile. additive, and a no-op where 008 ran
-- with it.
CREATE OR REPLACE FUNCTION evaluations_need_owners() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'an evaluation must name its assessment, classroom, organization and student (migration 008)'
    USING ERRCODE = 'object_not_in_prerequisite_state';
END $$;
CREATE OR REPLACE TRIGGER evaluations_owned BEFORE INSERT ON evaluations FOR EACH ROW
  WHEN (NEW.assessment_id IS NULL OR NEW.classroom_id IS NULL OR NEW.org_id IS NULL OR NEW.student_id IS NULL)
  EXECUTE FUNCTION evaluations_need_owners();
UPDATE evaluations e SET assessment_id=v.assessment_id,classroom_id=v.classroom_id,org_id=v.org_id,student_id=v.student_id
FROM (SELECT DISTINCT ON (evaluation_id) evaluation_id,assessment_id,classroom_id,org_id,student_id FROM evidence ORDER BY evaluation_id) v
WHERE v.evaluation_id=e.evaluation_id AND e.classroom_id IS NULL;
