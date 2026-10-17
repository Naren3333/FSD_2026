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

// errNotDraft is what a store returns when a change only a draft allows is asked of a published assessment.
var errNotDraft = errors.New("assessment is not a draft")

// errAlreadySubmitted is what saveAnswers returns once the student has submitted: their work is final.
var errAlreadySubmitted = errors.New("already submitted")

// errClassroomPurged is insertAssessment's answer for a classroom that has been deleted and purged.
var errClassroomPurged = errors.New("the classroom was deleted")

// store is a classroom's assessments and its students' submissions and saved work.
type store interface {
	findAssessment(ctx context.Context, org, id string) (Assessment, error)
	insertAssessment(ctx context.Context, org string, v Assessment) error
	// updateDraft replaces a draft's content; publishDraft publishes it. Both return errNotDraft once published.
	updateDraft(ctx context.Context, org string, v Assessment) error
	publishDraft(ctx context.Context, org, id string, at time.Time) error
	deleteDraft(ctx context.Context, org, id string) error
	// listAssessments lists the classroom's assessments for student (their submitted flag); drafts and assessments
	// not open yet only when all is set, for the classroom's teacher.
	listAssessments(ctx context.Context, org, classroomID, student string, all bool, now time.Time) ([]Summary, error)
	listProgress(ctx context.Context, org, classroomID string) ([]Progress, error)
	insertSubmission(ctx context.Context, v Assessment, sub Submission) (bool, error)
	findSaved(ctx context.Context, assessment, student string) (SavedAnswers, error)
	saveAnswers(ctx context.Context, assessment, student string, answers map[string]string) (time.Time, error)
}

// pgStore is the store in the service's own Postgres database.
type pgStore struct{ db *pgxpool.Pool }

const assessmentColumns = "id,classroom_id,title,questions,status,opens_at,due_at,late_policy,release_policy,ai_marking,published_at,coalesce(created_by,'')"

func scanAssessment(row pgx.Row) (Assessment, error) {
	var v Assessment
	var data []byte
	err := row.Scan(&v.ID, &v.Classroom, &v.Title, &data, &v.Status, &v.OpensAt, &v.DueAt, &v.LatePolicy, &v.ReleasePolicy, &v.AIMarking, &v.PublishedAt, &v.CreatedBy)
	if err == nil {
		v.Questions, err = decodeQuestions(data)
	}
	return v, err
}

// decodeQuestions reads stored questions. Those stored before questions had points are worth one each.
func decodeQuestions(data []byte) ([]Question, error) {
	var questions []Question
	if err := json.Unmarshal(data, &questions); err != nil {
		return nil, err
	}
	for i := range questions {
		if questions[i].Points == 0 {
			questions[i].Points = 1
		}
	}
	return questions, nil
}

func (p pgStore) findAssessment(ctx context.Context, org, id string) (Assessment, error) {
	return scanAssessment(p.db.QueryRow(ctx, "SELECT "+assessmentColumns+" FROM assessments WHERE id=$1 AND org_id=$2", id, org))
}

func (p pgStore) insertAssessment(ctx context.Context, org string, v Assessment) error {
	data, err := json.Marshal(v.Questions)
	if err != nil {
		return err
	}
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	// Shared with other inserts, exclusive against the classroom's purge: an insert either commits before the purge
	// (which then deletes it) or sees the classroom in purged_classrooms.
	if err = platform.LockShared(ctx, tx, platform.LockClassroom, v.Classroom); err != nil {
		return err
	}
	var purged bool
	if err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$1)", v.Classroom).Scan(&purged); err != nil {
		return err
	}
	if purged {
		return errClassroomPurged
	}
	if _, err = tx.Exec(ctx, "INSERT INTO assessments(id,classroom_id,org_id,title,questions,status,opens_at,due_at,late_policy,release_policy,ai_marking,published_at,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
		v.ID, v.Classroom, org, v.Title, data, v.Status, v.OpensAt, v.DueAt, v.LatePolicy, v.ReleasePolicy, v.AIMarking, v.PublishedAt, v.CreatedBy); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// draftChange runs one statement that applies to a draft only, and tells a published assessment (errNotDraft)
// from a missing one (platform.ErrNotFound).
func (p pgStore) draftChange(ctx context.Context, org, id, sql string, args ...any) error {
	result, err := p.db.Exec(ctx, sql, args...)
	if err != nil || result.RowsAffected() == 1 {
		return err
	}
	var status string
	if err = p.db.QueryRow(ctx, "SELECT status FROM assessments WHERE id=$1 AND org_id=$2", id, org).Scan(&status); err != nil {
		return err
	}
	return errNotDraft
}

func (p pgStore) updateDraft(ctx context.Context, org string, v Assessment) error {
	data, err := json.Marshal(v.Questions)
	if err != nil {
		return err
	}
	return p.draftChange(ctx, org, v.ID, "UPDATE assessments SET title=$3,questions=$4,status=$5,opens_at=$6,due_at=$7,late_policy=$8,release_policy=$9,ai_marking=$10,published_at=$11 WHERE id=$1 AND org_id=$2 AND status='draft'",
		v.ID, org, v.Title, data, v.Status, v.OpensAt, v.DueAt, v.LatePolicy, v.ReleasePolicy, v.AIMarking, v.PublishedAt)
}

func (p pgStore) publishDraft(ctx context.Context, org, id string, at time.Time) error {
	return p.draftChange(ctx, org, id, "UPDATE assessments SET status='published',published_at=$3 WHERE id=$1 AND org_id=$2 AND status='draft'", id, org, at)
}

func (p pgStore) deleteDraft(ctx context.Context, org, id string) error {
	return p.draftChange(ctx, org, id, "DELETE FROM assessments WHERE id=$1 AND org_id=$2 AND status='draft'", id, org)
}

// listProgress is the classroom's 100 newest assessments with how many students submitted each.
func (p pgStore) listProgress(ctx context.Context, org, classroomID string) ([]Progress, error) {
	rows, err := p.db.Query(ctx, "SELECT a.id,a.title,count(s.id),a.status,a.due_at FROM assessments a LEFT JOIN submissions s ON s.assessment_id=a.id WHERE a.classroom_id=$1 AND a.org_id=$2 GROUP BY a.id ORDER BY a.created_at DESC LIMIT 100", classroomID, org)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Progress, error) {
		var v Progress
		err := row.Scan(&v.ID, &v.Title, &v.Submissions, &v.Status, &v.DueAt)
		return v, err
	})
}

// listAssessments is the classroom's 100 newest assessments, each with whether student has submitted it. Without
// all, only those published and open. The question count and points are summed in SQL, so the questions (answer
// keys and rubrics included) are never read for a list; a question stored without points is worth one, as in
// decodeQuestions.
func (p pgStore) listAssessments(ctx context.Context, org, classroomID, student string, all bool, now time.Time) ([]Summary, error) {
	rows, err := p.db.Query(ctx, `SELECT a.id,a.title,EXISTS(SELECT 1 FROM submissions s WHERE s.assessment_id=a.id AND s.student_id=$3),
		a.status,a.opens_at,a.due_at,a.late_policy,jsonb_array_length(a.questions),
		(SELECT coalesce(sum(coalesce(nullif((q->>'points')::int,0),1)),0) FROM jsonb_array_elements(a.questions) q)
		FROM assessments a WHERE a.classroom_id=$1 AND a.org_id=$2 AND ($4 OR (a.status='published' AND (a.opens_at IS NULL OR a.opens_at<=$5)))
		ORDER BY a.created_at DESC LIMIT 100`, classroomID, org, student, all, now)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Summary, error) {
		var v Summary
		err := row.Scan(&v.ID, &v.Title, &v.Submitted, &v.Status, &v.OpensAt, &v.DueAt, &v.LatePolicy, &v.Questions, &v.Points)
		return v, err
	})
}

// insertSubmission stores the submission, removes the student's saved work and emits the outbox event in one
// transaction. It reports inserted=false (with a nil error) when the student has already submitted this
// assessment.
func (p pgStore) insertSubmission(ctx context.Context, v Assessment, sub Submission) (bool, error) {
	answers, err := json.Marshal(sub.Answers)
	if err != nil {
		return false, err
	}
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	if err = lockStudentWork(ctx, tx, v.ID, sub.Student); err != nil {
		return false, err
	}
	result, err := tx.Exec(ctx, "INSERT INTO submissions(id,assessment_id,student_id,answers,late,created_at) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(assessment_id,student_id) DO NOTHING", sub.ID, v.ID, sub.Student, answers, sub.Late, sub.SubmittedAt)
	if err != nil {
		return false, err
	}
	if result.RowsAffected() == 0 {
		return false, nil
	}
	if _, err = tx.Exec(ctx, "DELETE FROM saved_answers WHERE assessment_id=$1 AND student_id=$2", v.ID, sub.Student); err != nil {
		return false, err
	}
	err = platform.Emit(ctx, tx, topicSubmitted, sub.ID, submittedEvent(v, sub))
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// findSaved is the student's saved work, or no answers when they have saved nothing.
func (p pgStore) findSaved(ctx context.Context, assessment, student string) (SavedAnswers, error) {
	saved := SavedAnswers{Answers: map[string]string{}}
	var data []byte
	var at time.Time
	err := p.db.QueryRow(ctx, "SELECT answers,saved_at FROM saved_answers WHERE assessment_id=$1 AND student_id=$2", assessment, student).Scan(&data, &at)
	if errors.Is(err, pgx.ErrNoRows) {
		return saved, nil
	}
	if err == nil {
		saved.SavedAt = &at
		err = json.Unmarshal(data, &saved.Answers)
	}
	return saved, err
}

// lockStudentWork serializes one student's saving and submitting of one assessment until tx ends, so saved work
// cannot be written after the submission that deletes it.
func lockStudentWork(ctx context.Context, tx pgx.Tx, assessment, student string) error {
	return platform.Lock(ctx, tx, platform.LockStudentWork, assessment+"/"+student)
}

// saveAnswers replaces the student's saved work, unless they have already submitted (errAlreadySubmitted).
func (p pgStore) saveAnswers(ctx context.Context, assessment, student string, answers map[string]string) (time.Time, error) {
	data, err := json.Marshal(answers)
	if err != nil {
		return time.Time{}, err
	}
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return time.Time{}, err
	}
	defer tx.Rollback(ctx)
	if err = lockStudentWork(ctx, tx, assessment, student); err != nil {
		return time.Time{}, err
	}
	var at time.Time
	err = tx.QueryRow(ctx, `INSERT INTO saved_answers(assessment_id,student_id,answers) SELECT $1,$2,$3
		WHERE NOT EXISTS(SELECT 1 FROM submissions WHERE assessment_id=$1 AND student_id=$2)
		ON CONFLICT(assessment_id,student_id) DO UPDATE SET answers=excluded.answers,saved_at=now() RETURNING saved_at`, assessment, student, data).Scan(&at)
	if errors.Is(err, pgx.ErrNoRows) {
		return at, errAlreadySubmitted
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	return at, err
}
