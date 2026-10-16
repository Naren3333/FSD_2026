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
	// listEvaluations is the classroom's evaluations, or one assessment's when assessment is set.
	listEvaluations(ctx context.Context, org, classroom, assessment string) ([]Evaluation, error)
	listReleased(ctx context.Context, org, classroom, student string) ([]Evaluation, error)
	countEvaluations(ctx context.Context, org, classroom string) (Counts, error)
	findClassroom(ctx context.Context, org, id string) (string, error)
	assessmentClassroom(ctx context.Context, org, assessment string) (string, error)
	saveMarks(ctx context.Context, org, id, actor string, marks map[string]Mark) (Evaluation, error)
	finalizeEvaluation(ctx context.Context, org, id, actor, reason string, marks map[string]Mark) (Evaluation, error)
	release(ctx context.Context, org, assessment, actor string) (int, error)
}

// pgStore is the store in the service's own Postgres database.
type pgStore struct{ db *pgxpool.Pool }

// Counts is how many of a classroom's evaluations are drafts waiting for its teacher, how many are finalized, and
// how many finalized ones its students cannot see until the teacher releases them.
type Counts struct {
	Drafts     int `json:"drafts"`
	Finalized  int `json:"finalized"`
	Unreleased int `json:"unreleased"`
}

func (p pgStore) countEvaluations(ctx context.Context, org, classroom string) (Counts, error) {
	var c Counts
	err := p.db.QueryRow(ctx, "SELECT count(*) FILTER (WHERE status='draft'),count(*) FILTER (WHERE status='finalized'),count(*) FILTER (WHERE status='finalized' AND released_at IS NULL) FROM evaluations WHERE classroom_id=$1 AND org_id=$2", classroom, org).Scan(&c.Drafts, &c.Finalized, &c.Unreleased)
	return c, err
}

// insertEvaluation runs inside the event consumer's transaction. A repeated submission changes nothing, and nor
// does one for a classroom already purged. queued are the questions that get an AI suggestion.
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
	result, err := tx.Exec(ctx, `INSERT INTO evaluations(id,assessment_id,classroom_id,org_id,student_id,evidence,questions,answers,late,submitted_at,release_policy,teacher_id)
		SELECT $1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::jsonb,$7::jsonb,$8::jsonb,$9::boolean,$10::timestamptz,$11::text,$12::text
		WHERE NOT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$3) ON CONFLICT(id) DO NOTHING`,
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

// evaluationColumns are the columns scanEvaluation reads first.
const evaluationColumns = "id,assessment_id,classroom_id,student_id,status,evidence,coalesce(answers,'{}'::jsonb),late,submitted_at,release_policy,finalized_at,released_at"

// scanEvaluation reads evaluationColumns, then any extra columns into more, and decodes the evidence. It also
// returns the evidence as stored.
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

// listEvaluations is the classroom's 100 newest evaluations, or up to 500 of one assessment's, oldest first.
func (p pgStore) listEvaluations(ctx context.Context, org, classroom, assessment string) ([]Evaluation, error) {
	if assessment != "" {
		return p.collect(p.db.Query(ctx, "SELECT "+evaluationColumns+" FROM evaluations WHERE classroom_id=$1 AND org_id=$2 AND assessment_id=$3 ORDER BY created_at LIMIT 500", classroom, org, assessment))
	}
	return p.collect(p.db.Query(ctx, "SELECT "+evaluationColumns+" FROM evaluations WHERE classroom_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 100", classroom, org))
}

// listReleased is one student's released results in a classroom, newest first. Drafts and unreleased grades never
// leave Grading for students: a grade is the student's only once a teacher has finalized and released it.
func (p pgStore) listReleased(ctx context.Context, org, classroom, student string) ([]Evaluation, error) {
	return p.collect(p.db.Query(ctx, "SELECT "+evaluationColumns+" FROM evaluations WHERE classroom_id=$1 AND org_id=$2 AND student_id=$3 AND status='finalized' AND released_at IS NOT NULL ORDER BY released_at DESC LIMIT 100", classroom, org, student))
}

func (p pgStore) findClassroom(ctx context.Context, org, id string) (string, error) {
	var classroom string
	err := p.db.QueryRow(ctx, "SELECT classroom_id FROM evaluations WHERE id=$1 AND org_id=$2", id, org).Scan(&classroom)
	return classroom, err
}

// assessmentClassroom is the classroom of an assessment that has evaluations; platform.ErrNotFound when none.
func (p pgStore) assessmentClassroom(ctx context.Context, org, assessment string) (string, error) {
	var classroom string
	err := p.db.QueryRow(ctx, "SELECT classroom_id FROM evaluations WHERE assessment_id=$1 AND org_id=$2 LIMIT 1", assessment, org).Scan(&classroom)
	return classroom, err
}

// lockDraft locks a draft evaluation with its questions for a change. It returns errAlreadyFinalized for a
// finalized one, and the evidence as stored.
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

// saveMarks records the teacher's marks on a draft without finalizing it, with an audit entry.
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

// finalizeEvaluation locks the evaluation row, applies the marks, records the audit entry and emits the finalized
// event in one transaction, releasing the grade to the student at once when its assessment says so. It returns
// errAlreadyFinalized, errUnmarked or a marking error for the business failures; any other error is an
// infrastructure failure.
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
	if err == nil {
		err = platform.Emit(ctx, tx, topicFinalized, v.ID, map[string]any{"evaluation_id": v.ID, "assessment_id": v.Assessment, "classroom_id": v.Classroom, "org_id": org, "student_id": v.Student, "evidence": final(v.Evidence)})
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	return v, err
}

// release shows the assessment's finalized, unreleased grades to their students, with an audit entry each, and
// returns how many it released. Drafts stay drafts: finalizing one later releases it only if its assessment
// releases on finalization, or at the next release.
func (p pgStore) release(ctx context.Context, org, assessment, actor string) (int, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	rows, err := tx.Query(ctx, "UPDATE evaluations SET released_at=now() WHERE assessment_id=$1 AND org_id=$2 AND status='finalized' AND released_at IS NULL RETURNING id,evidence", assessment, org)
	if err != nil {
		return 0, err
	}
	type released struct {
		id       string
		evidence []byte
	}
	list, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (released, error) {
		var r released
		err := row.Scan(&r.id, &r.evidence)
		return r, err
	})
	if err != nil {
		return 0, err
	}
	for _, r := range list {
		if _, err = tx.Exec(ctx, "INSERT INTO audit(evaluation_id,actor_id,action,reason,before_evidence,after_evidence) VALUES($1,$2,'release','Results released',$3,$3)", r.id, actor, r.evidence); err != nil {
			return 0, err
		}
	}
	return len(list), tx.Commit(ctx)
}
