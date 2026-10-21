package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var errAlreadyFinalized = errors.New("evaluation already finalized")

// store is a classroom's draft and finalized evaluations, with their audit trail.
type store interface {
	// listevaluations is one page of the classroom's evaluations of the given students, or of one assessment's
	// when assessment is set, with how many there are in all.
	listEvaluations(ctx context.Context, org, classroom, assessment string, students []string, offset, limit int) ([]Evaluation, int, error)
	listReleased(ctx context.Context, org, classroom, student string) ([]Evaluation, error)
	// countevaluations counts the classroom's evaluations of the given students, or one assessment's when
	// assessment is set.
	countEvaluations(ctx context.Context, org, classroom, assessment string, students []string) (Counts, error)
	findEvaluation(ctx context.Context, org, id string) (Evaluation, error)
	findClassroom(ctx context.Context, org, id string) (string, error)
	assessmentClassroom(ctx context.Context, org, assessment string) (string, error)
	saveMarks(ctx context.Context, org, id, actor string, marks map[string]Mark) (Evaluation, error)
	finalizeEvaluation(ctx context.Context, org, id, actor, reason string, marks map[string]Mark) (Evaluation, error)
	// release releases the assessment's finalized grades of the given students.
	release(ctx context.Context, org, assessment, actor string, students []string) (int, error)
}

// pgstore is the store in the service's own postgres database.
type pgStore struct{ db *pgxpool.Pool }

// counts is how many of a classroom's evaluations are drafts waiting for its teacher, how many are
// finalized, and how many finalized ones its students cannot see until the teacher releases them.
type Counts struct {
	Drafts     int `json:"drafts"`
	Finalized  int `json:"finalized"`
	Unreleased int `json:"unreleased"`
}

func (p pgStore) countEvaluations(ctx context.Context, org, classroom, assessment string, students []string) (Counts, error) {
	var c Counts
	err := p.db.QueryRow(ctx, "SELECT count(*) FILTER (WHERE status='draft'),count(*) FILTER (WHERE status='finalized'),count(*) FILTER (WHERE status='finalized' AND released_at IS NULL) FROM evaluations WHERE classroom_id=$1 AND org_id=$2 AND student_id=ANY($3::text[]) AND ($4::uuid IS NULL OR assessment_id=$4::uuid)", classroom, org, students, optional(assessment)).Scan(&c.Drafts, &c.Finalized, &c.Unreleased)
	return c, err
}

// optional is an assessment id for a query, or null (every assessment) for none.
func optional(assessment string) *string {
	if assessment == "" {
		return nil
	}
	return &assessment
}

// insertevaluation runs inside the event consumer's transaction.
func insertEvaluation(ctx context.Context, tx pgx.Tx, v Submitted, evidence []Evidence, queued []string) error {
	data, err := json.Marshal(evidence)
	if err != nil {
		return err
	}
	questions, err := json.Marshal(v.Questions)
	if err != nil {
		return err
	}
	answers, err := json.Marshal(v.Answers)
	if err != nil {
		return err
	}
	var teacher *string
	if v.marking() {
		teacher = &v.Teacher
	}
	// shared with other submissions to the assessment, exclusive against its purge (purgeassessment), and
	// likewise with the student's other work against the purge of their deleted account (purgeuser).
	if err = platform.LockShared(ctx, tx, platform.LockAssessment, v.Assessment); err != nil {
		return err
	}
	if err = platform.LockShared(ctx, tx, platform.LockUser, v.Student); err != nil {
		return err
	}
	// a due date changed since the student submitted (changedue) decides whether the work is late.
	result, err := tx.Exec(ctx, `INSERT INTO evaluations(id,assessment_id,classroom_id,org_id,student_id,evidence,questions,answers,late,submitted_at,release_policy,teacher_id)
		SELECT $1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::jsonb,$7::jsonb,$8::jsonb,
			CASE WHEN d.assessment_id IS NULL OR $10::timestamptz IS NULL THEN $9::boolean ELSE coalesce($10::timestamptz>d.due_at,false) END,
			$10::timestamptz,$11::text,$12::text
		FROM (SELECT 1) one LEFT JOIN assessment_due d ON d.assessment_id=$2::uuid
		WHERE NOT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$3) AND NOT EXISTS(SELECT 1 FROM deleted_assessments WHERE assessment_id=$2)
			AND NOT EXISTS(SELECT 1 FROM deleted_users WHERE user_id=$5)
		ON CONFLICT(id) DO NOTHING`,
		v.Submission, v.Assessment, v.Classroom, v.Org, v.Student, data, questions, answers, v.Late, v.SubmittedAt, v.ReleasePolicy, teacher)
	if err != nil || result.RowsAffected() == 0 {
		return err
	}
	for _, question := range queued {
		if _, err = tx.Exec(ctx, "INSERT INTO marking_jobs(evaluation_id,question_id) VALUES($1,$2) ON CONFLICT DO NOTHING", v.Submission, question); err != nil {
			return err
		}
	}
	return nil
}

// evaluationcolumns are the columns scanevaluation reads first.
const evaluationColumns = "id,assessment_id,classroom_id,student_id,status,evidence,coalesce(answers,'{}'::jsonb),late,submitted_at,release_policy,finalized_at,released_at"

// scanevaluation reads evaluationcolumns, then any extra columns into more, and decodes the evidence.
func scanEvaluation(row pgx.Row, more ...any) (Evaluation, []byte, error) {
	var v Evaluation
	var data, answers []byte
	columns := make([]any, 0, 12+len(more))
	columns = append(columns, &v.ID, &v.Assessment, &v.Classroom, &v.Student, &v.Status, &data, &answers, &v.Late, &v.SubmittedAt, &v.ReleasePolicy, &v.FinalizedAt, &v.ReleasedAt)
	if err := row.Scan(append(columns, more...)...); err != nil {
		return v, nil, err
	}
	if err := json.Unmarshal(answers, &v.Answers); err != nil {
		return v, nil, err
	}
	return v, data, json.Unmarshal(data, &v.Evidence)
}

func (p pgStore) collect(rows pgx.Rows, err error) ([]Evaluation, error) {
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Evaluation, error) {
		v, _, err := scanEvaluation(row)
		return v, err
	})
}

// listevaluations is one page of the classroom's evaluations of the given students, newest first, or of one
// assessment's, oldest first, with how many there are in all.
func (p pgStore) listEvaluations(ctx context.Context, org, classroom, assessment string, students []string, offset, limit int) ([]Evaluation, int, error) {
	const match = " FROM evaluations WHERE classroom_id=$1 AND org_id=$2 AND student_id=ANY($3::text[]) AND ($4::uuid IS NULL OR assessment_id=$4::uuid)"
	order := " ORDER BY created_at DESC,id DESC"
	if assessment != "" {
		order = " ORDER BY created_at,id"
	}
	var total int
	if err := p.db.QueryRow(ctx, "SELECT count(*)"+match, classroom, org, students, optional(assessment)).Scan(&total); err != nil {
		return nil, 0, err
	}
	list, err := p.collect(p.db.Query(ctx, "SELECT "+evaluationColumns+match+order+" OFFSET $5 LIMIT $6", classroom, org, students, optional(assessment), offset, limit))
	return list, total, err
}

// findevaluation is one evaluation with its students' answers; platform.errnotfound when there is none.
func (p pgStore) findEvaluation(ctx context.Context, org, id string) (Evaluation, error) {
	v, _, err := scanEvaluation(p.db.QueryRow(ctx, "SELECT "+evaluationColumns+" FROM evaluations WHERE id=$1 AND org_id=$2", id, org))
	return v, err
}

// listreleased is one student's released results in a classroom, newest first.
func (p pgStore) listReleased(ctx context.Context, org, classroom, student string) ([]Evaluation, error) {
	return p.collect(p.db.Query(ctx, "SELECT "+evaluationColumns+" FROM evaluations WHERE classroom_id=$1 AND org_id=$2 AND student_id=$3 AND status='finalized' AND released_at IS NOT NULL ORDER BY released_at DESC LIMIT 100", classroom, org, student))
}

func (p pgStore) findClassroom(ctx context.Context, org, id string) (string, error) {
	var classroom string
	err := p.db.QueryRow(ctx, "SELECT classroom_id FROM evaluations WHERE id=$1 AND org_id=$2", id, org).Scan(&classroom)
	return classroom, err
}

// assessmentclassroom is the classroom of an assessment that has evaluations; platform.errnotfound when
// none.
func (p pgStore) assessmentClassroom(ctx context.Context, org, assessment string) (string, error) {
	var classroom string
	err := p.db.QueryRow(ctx, "SELECT classroom_id FROM evaluations WHERE assessment_id=$1 AND org_id=$2 LIMIT 1", assessment, org).Scan(&classroom)
	return classroom, err
}

// lockdraft locks a draft evaluation with its questions for a change.
func lockDraft(ctx context.Context, tx pgx.Tx, org, id string) (Evaluation, []Question, []byte, error) {
	var data []byte
	v, before, err := scanEvaluation(tx.QueryRow(ctx, "SELECT "+evaluationColumns+",coalesce(questions,'[]'::jsonb) FROM evaluations WHERE id=$1 AND org_id=$2 FOR UPDATE", id, org), &data)
	if err != nil {
		return v, nil, nil, err
	}
	if v.Status != statusDraft {
		return v, nil, nil, errAlreadyFinalized
	}
	var questions []Question
	return v, questions, before, json.Unmarshal(data, &questions)
}

// savemarks records the teacher's marks on a draft without finalizing it, with an audit entry.
func (p pgStore) saveMarks(ctx context.Context, org, id, actor string, marks map[string]Mark) (Evaluation, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return Evaluation{}, err
	}
	defer tx.Rollback(ctx)
	v, questions, before, err := lockDraft(ctx, tx, org, id)
	if err != nil {
		return v, err
	}
	if err = applyMarks(v.Evidence, questions, marks); err != nil {
		return v, err
	}
	after, err := json.Marshal(v.Evidence)
	if err != nil {
		return v, err
	}
	_, err = tx.Exec(ctx, "UPDATE evaluations SET evidence=$1 WHERE id=$2", after, v.ID)
	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO audit(evaluation_id,actor_id,action,reason,before_evidence,after_evidence) VALUES($1,$2,'mark','Marks saved',$3,$4)", v.ID, actor, before, after)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	return v, err
}

// finalize grades and audit atomically; emit results only when released to the student.
func (p pgStore) finalizeEvaluation(ctx context.Context, org, id, actor, reason string, marks map[string]Mark) (Evaluation, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return Evaluation{}, err
	}
	defer tx.Rollback(ctx)
	v, questions, before, err := lockDraft(ctx, tx, org, id)
	if err != nil {
		return v, err
	}
	if err = applyMarks(v.Evidence, questions, marks); err != nil {
		return v, err
	}
	if unmarked(v.Evidence) {
		return v, errUnmarked
	}
	after, err := json.Marshal(v.Evidence)
	if err != nil {
		return v, err
	}
	now := time.Now().UTC()
	v.Status, v.FinalizedAt = statusFinalized, &now
	if v.ReleasePolicy == releaseOnFinalize {
		v.ReleasedAt = &now
	}
	_, err = tx.Exec(ctx, "UPDATE evaluations SET evidence=$1,status='finalized',finalized_by=$2,finalized_at=$3,released_at=$4 WHERE id=$5", after, actor, now, v.ReleasedAt, v.ID)
	if err == nil {
		_, err = tx.Exec(ctx, "DELETE FROM marking_jobs WHERE evaluation_id=$1", v.ID)
	}
	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO audit(evaluation_id,actor_id,action,reason,before_evidence,after_evidence) VALUES($1,$2,'finalize',$3,$4,$5)", v.ID, actor, reason, before, after)
	}
	if err == nil && v.ReleasedAt != nil {
		err = emitFinalized(ctx, tx, org, v)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	return v, err
}

// publish released grades through the outbox so analytics cannot reveal drafts.
func emitFinalized(ctx context.Context, tx pgx.Tx, org string, v Evaluation) error {
	return platform.Emit(ctx, tx, topicFinalized, v.ID, map[string]any{"evaluation_id": v.ID, "assessment_id": v.Assessment, "classroom_id": v.Classroom, "org_id": org, "student_id": v.Student, "evidence": final(v.Evidence)})
}

// release shows the assessment's finalized, unreleased grades to their students, with an audit entry and a
// grading.finalized.v3 event each, and returns how many it released.
func (p pgStore) release(ctx context.Context, org, assessment, actor string, students []string) (int, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	rows, err := tx.Query(ctx, "UPDATE evaluations SET released_at=now() WHERE assessment_id=$1 AND org_id=$2 AND student_id=ANY($3::text[]) AND status='finalized' AND released_at IS NULL RETURNING "+evaluationColumns, assessment, org, students)
	if err != nil {
		return 0, err
	}
	type released struct {
		v        Evaluation
		evidence []byte
	}
	list, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (released, error) {
		v, evidence, err := scanEvaluation(row)
		return released{v, evidence}, err
	})
	if err != nil {
		return 0, err
	}
	for _, r := range list {
		if _, err = tx.Exec(ctx, "INSERT INTO audit(evaluation_id,actor_id,action,reason,before_evidence,after_evidence) VALUES($1,$2,'release','Results released',$3,$3)", r.v.ID, actor, r.evidence); err != nil {
			return 0, err
		}
		if err = emitFinalized(ctx, tx, org, r.v); err != nil {
			return 0, err
		}
	}
	return len(list), tx.Commit(ctx)
}
