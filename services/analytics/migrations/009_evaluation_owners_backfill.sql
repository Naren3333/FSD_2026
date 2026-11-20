-- fills 008's owners from the evidence of the evaluations that still have some, under row locks only, so
-- reads go on meanwhile. one withdrawn before 008 has no evidence to fill it from and keeps only its ids and
-- revision, no student data; it gains its owners when its correction is finalized again.
UPDATE evaluations e SET assessment_id=v.assessment_id,classroom_id=v.classroom_id,org_id=v.org_id,student_id=v.student_id
FROM (SELECT DISTINCT ON (evaluation_id) evaluation_id,assessment_id,classroom_id,org_id,student_id FROM evidence ORDER BY evaluation_id) v
WHERE v.evaluation_id=e.evaluation_id AND e.classroom_id IS NULL;
