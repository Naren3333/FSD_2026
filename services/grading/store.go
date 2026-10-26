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

var (
	errAlreadyFinalized = errors.New("evaluation already finalized")
	errNotFinalized     = errors.New("evaluation is not finalized")
)

// store is a classroom's draft and finalized evaluations, with their audit trail.
type store interface {
	// listEvaluations is one page of the classroom's evaluations of the given students, or of one assessment's
	// when assessment is set, with how many there are in all.
	listEvaluations(ctx context.Context, org, classroom, assessment string, students []string, offset, limit int) ([]Evaluation, int, error)
	// listReleased is the student's 100 newest released results, or the page paging asks for.
	listReleased(ctx context.Context, org, classroom, student string, paging ...platform.ListWindow) ([]Evaluation, error)
	countReleased(ctx context.Context, org, classroom, student string) (int, error)
	// countEvaluations counts the classroom's evaluations of the given students, or one assessment's when
	// assessment is set.
	countEvaluations(ctx context.Context, org, classroom, assessment string, students []string) (Counts, error)
	findEvaluation(ctx context.Context, org, id string) (Evaluation, error)
	// findClassroom is the classroom and student of an evaluation.
	findClassroom(ctx context.Context, org, id string) (classroom, student string, err error)
	assessmentClassroom(ctx context.Context, org, assessment string) (string, error)
	saveMarks(ctx context.Context, org, id, actor string, marks map[string]Mark) (Evaluation, error)
	finalizeEvaluation(ctx context.Context, org, id, actor, reason string, marks map[string]Mark) (Evaluation, error)
	// finalizeAssessment finalizes the assessment's drafts of the given students that need no more marking.
	finalizeAssessment(ctx context.Context, org, assessment, actor, reason string, students []string) (finalized, waiting int, err error)
	reopenEvaluation(ctx context.Context, org, id, actor, reason string) (Evaluation, error)
	// release releases the assessment's finalized grades of the given students.
	release(ctx context.Context, org, assessment, actor string, students []string) (int, error)
}

// pgStore is the store in the service's own postgres database.
type pgStore struct{ db *pgxpool.Pool }

// counts is how many of a classroom's evaluations are drafts waiting for its teacher, how many are
// finalized, how many finalized ones its students cannot see until the teacher releases them, and how many
// drafts need no more marking, so finalizeAssessment would finalize them.
type Counts struct {
	Drafts     int `json:"drafts"`
	Finalized  int `json:"finalized"`
	Unreleased int `json:"unreleased"`
	Ready      int `json:"ready"`
}

func (p pgStore) countEvaluations(ctx context.Context, org, classroom, assessment string, students []string) (Counts, error) {
	var c Counts
	err := p.db.QueryRow(ctx, "SELECT count(*) FILTER (WHERE status='draft'),count(*) FILTER (WHERE status='finalized'),count(*) FILTER (WHERE status='finalized' AND released_at IS NULL),count(*) FILTER (WHERE status='draft' AND NOT jsonb_path_exists(evidence,'$[*] ? (@.needs_marking == true)')) FROM evaluations WHERE classroom_id=$1 AND org_id=$2 AND student_id=ANY($3::text[]) AND ($4::uuid IS NULL OR assessment_id=$4::uuid)", classroom, org, students, optional(assessment)).Scan(&c.Drafts, &c.Finalized, &c.Unreleased, &c.Ready)
	return c, err
}

// optional is an assessment id for a query, or null (every assessment) for none.
func optional(assessment string) *string {
	if assessment == "" {
		return nil
	}
	return &assessment
}

// insertEvaluation runs inside the event consumer's transaction.
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
	// shared with other submissions to the assessment, exclusive against its purge (purgeAssessment), and
	// likewise with the student's other work against the purge of their deleted account (purgeUser).
	if err = platform.LockShared(ctx, tx, platform.LockAssessment, v.Assessment); err != nil {
		return err
	}
	if err = platform.LockShared(ctx, tx, platform.LockUser, v.Student); err != nil {
		return err
	}
	// a due date changed since the student submitted (changeDue) decides whether the work is late.
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

// evaluationColumns are the columns scanEvaluation reads first.
const evaluationColumns = "id,assessment_id,classroom_id,student_id,status,evidence,coalesce(answers,'{}'::jsonb),late,submitted_at,release_policy,finalized_at,released_at,revision"

// scanEvaluation reads evaluationColumns, then any extra columns into more, and decodes the evidence.
func scanEvaluation(row pgx.Row, more ...any) (Evaluation, []byte, error) {
	var v Evaluation
	var data, answers []byte
	columns := make([]any, 0, 13+len(more))
	columns = append(columns, &v.ID, &v.Assessment, &v.Classroom, &v.Student, &v.Status, &data, &answers, &v.Late, &v.SubmittedAt, &v.ReleasePolicy, &v.FinalizedAt, &v.ReleasedAt, &v.Revision)
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

// listEvaluations is one page of the classroom's evaluations of the given students, newest first, or of one
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

// findEvaluation is one evaluation with its students' answers; platform.ErrNotFound when there is none.
func (p pgStore) findEvaluation(ctx context.Context, org, id string) (Evaluation, error) {
	v, _, err := scanEvaluation(p.db.QueryRow(ctx, "SELECT "+evaluationColumns+" FROM evaluations WHERE id=$1 AND org_id=$2", id, org))
	return v, err
}

// releasedMatch picks one student's released results in a classroom.
const releasedMatch = " FROM evaluations WHERE classroom_id=$1 AND org_id=$2 AND student_id=$3 AND status='finalized' AND released_at IS NOT NULL"

// listReleased is one student's released results in a classroom, newest first.
func (p pgStore) listReleased(ctx context.Context, org, classroom, student string, paging ...platform.ListWindow) ([]Evaluation, error) {
	window := platform.Window(paging, 100)
	return p.collect(p.db.Query(ctx, "SELECT "+evaluationColumns+releasedMatch+" ORDER BY released_at DESC,id DESC OFFSET $4 LIMIT $5", classroom, org, student, window.Offset, window.Limit))
}

func (p pgStore) countReleased(ctx context.Context, org, classroom, student string) (int, error) {
	var total int
	err := p.db.QueryRow(ctx, "SELECT count(*)"+releasedMatch, classroom, org, student).Scan(&total)
	return total, err
}

func (p pgStore) findClassroom(ctx context.Context, org, id string) (classroom, student string, err error) {
	err = p.db.QueryRow(ctx, "SELECT classroom_id,student_id FROM evaluations WHERE id=$1 AND org_id=$2", id, org).Scan(&classroom, &student)
	return classroom, student, err
}

// assessmentClassroom is the classroom of an assessment that has evaluations; platform.ErrNotFound when
// none.
func (p pgStore) assessmentClassroom(ctx context.Context, org, assessment string) (string, error) {
	var classroom string
	err := p.db.QueryRow(ctx, "SELECT classroom_id FROM evaluations WHERE assessment_id=$1 AND org_id=$2 LIMIT 1", assessment, org).Scan(&classroom)
	return classroom, err
}

// lockDraft locks a draft evaluation with its questions for a change.
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
	if v, err = finalizeLocked(ctx, tx, org, actor, reason, v, before); err == nil {
		err = tx.Commit(ctx)
	}
	return v, err
}

// finalizeLocked finalizes a draft locked in tx with its evidence as marked, with an audit entry, and
// publishes it when released.
func finalizeLocked(ctx context.Context, tx pgx.Tx, org, actor, reason string, v Evaluation, before []byte) (Evaluation, error) {
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
	return v, err
}

// finalizeAssessment finalizes, in one transaction, the assessment's drafts of the given students that need
// no more marking, and returns how many it finalized and how many still need marking.
func (p pgStore) finalizeAssessment(ctx context.Context, org, assessment, actor, reason string, students []string) (finalized, waiting int, err error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return 0, 0, err
	}
	defer tx.Rollback(ctx)
	// locked in id order, as concurrent bulk finalizes of the same assessment take them.
	rows, err := tx.Query(ctx, "SELECT "+evaluationColumns+" FROM evaluations WHERE assessment_id=$1 AND org_id=$2 AND student_id=ANY($3::text[]) AND status='draft' ORDER BY id FOR UPDATE", assessment, org, students)
	if err != nil {
		return 0, 0, err
	}
	type draft struct {
		v        Evaluation
		evidence []byte
	}
	list, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (draft, error) {
		v, evidence, err := scanEvaluation(row)
		return draft{v, evidence}, err
	})
	if err != nil {
		return 0, 0, err
	}
	for _, d := range list {
		if unmarked(d.v.Evidence) {
			waiting++
			continue
		}
		if _, err = finalizeLocked(ctx, tx, org, actor, reason, d.v, d.evidence); err != nil {
			return 0, 0, err
		}
		finalized++
	}
	return finalized, waiting, tx.Commit(ctx)
}

// reopenEvaluation turns a finalized grade back into a draft at its next revision, with an audit entry, and
// withdraws it from analytics when it had been released (adr 0016).
func (p pgStore) reopenEvaluation(ctx context.Context, org, id, actor, reason string) (Evaluation, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return Evaluation{}, err
	}
	defer tx.Rollback(ctx)
	v, evidence, err := scanEvaluation(tx.QueryRow(ctx, "SELECT "+evaluationColumns+" FROM evaluations WHERE id=$1 AND org_id=$2 FOR UPDATE", id, org))
	if err != nil {
		return v, err
	}
	if v.Status != statusFinalized {
		return v, errNotFinalized
	}
	released := v.ReleasedAt != nil
	v.Status, v.FinalizedAt, v.ReleasedAt, v.Revision = statusDraft, nil, nil, v.Revision+1
	_, err = tx.Exec(ctx, "UPDATE evaluations SET status='draft',finalized_by=NULL,finalized_at=NULL,released_at=NULL,revision=$1 WHERE id=$2", v.Revision, v.ID)
	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO audit(evaluation_id,actor_id,action,reason,before_evidence,after_evidence) VALUES($1,$2,'reopen',$3,$4,$4)", v.ID, actor, reason, evidence)
	}
	if err == nil && released {
		err = platform.Emit(ctx, tx, topicReopened, v.ID, Reopened{v.ID, v.Assessment, v.Classroom, org, v.Student, v.Revision})
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	return v, err
}

// Reopened is grading.reopened.v1: a released grade went back to being a draft at revision.
type Reopened struct {
	Evaluation string `json:"evaluation_id"`
	Assessment string `json:"assessment_id"`
	Classroom  string `json:"classroom_id"`
	Org        string `json:"org_id"`
	Student    string `json:"student_id"`
	Revision   int    `json:"revision"`
}

// publish released grades through the outbox so analytics cannot reveal drafts; the revision lets it keep
// only the newest (adr 0016).
func emitFinalized(ctx context.Context, tx pgx.Tx, org string, v Evaluation) error {
	return platform.Emit(ctx, tx, topicFinalized, v.ID, map[string]any{"evaluation_id": v.ID, "assessment_id": v.Assessment, "classroom_id": v.Classroom, "org_id": org, "student_id": v.Student, "evidence": final(v.Evidence), "revision": v.Revision})
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
