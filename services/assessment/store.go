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

// errHasSubmissions is what a store returns when a change that needs an assessment nobody has submitted is asked of
// one that a student has: its questions and marking settings are what their answers were graded against.
var errHasSubmissions = errors.New("assessment has submissions")

// errAssessmentChanged is insertSubmission's answer when the teacher changed the questions or marking settings
// after the student opened the assessment: the answers are to questions it no longer has. updateAssessment returns it
// when another change landed after the one being saved was read.
var errAssessmentChanged = errors.New("assessment changed")

// errClosed is insertSubmission's answer when the assessment, as it is when the submission commits, is past its due
// date and refuses late work: the teacher may have just changed either.
var errClosed = errors.New("assessment closed")

// errAlreadySubmitted is what saveAnswers returns once the student has submitted: their work is final.
var errAlreadySubmitted = errors.New("already submitted")

// errClassroomPurged is insertAssessment's answer for a classroom that has been deleted and purged.
var errClassroomPurged = errors.New("the classroom was deleted")

// store is a classroom's assessments and its students' submissions and saved work.
type store interface {
	findAssessment(ctx context.Context, org, id string) (Assessment, error)
	insertAssessment(ctx context.Context, org string, v Assessment) error
	// updateAssessment replaces before, the assessment as the teacher's change was made against, with v while no
	// student has submitted it (errHasSubmissions after), and never turns a published one back into a draft
	// (errNotDraft). It returns errAssessmentChanged when the stored questions or marking settings are no longer
	// before's: v's question ids were chosen against them. A new due date of a published assessment is announced
	// (topicDueChanged), as by updateDetails. publishDraft publishes a draft and returns errNotDraft once
	// published.
	updateAssessment(ctx context.Context, org string, before, v Assessment) error
	publishDraft(ctx context.Context, org, id string, at time.Time) error
	// updateDetails changes the title, due date and late policy at any time. A new due date also decides again
	// which submissions are late, here and (topicDueChanged) in Grading.
	updateDetails(ctx context.Context, org, id string, d Details) error
	// deleteAssessment deletes an assessment with any work students saved on it. Once a student has submitted it
	// (errHasSubmissions), only with submissions set: then its submissions go too, and topicDeleted has Grading and
	// Analytics delete the grades and evidence they hold for it.
	deleteAssessment(ctx context.Context, org, id string, submissions bool) error
	// listAssessments lists the classroom's assessments for student (their submitted flag); drafts and assessments
	// not open yet only when all is set, for the classroom's teacher, with how many of the enrolled students given
	// have submitted each. listProgress counts those students' submissions too.
	listAssessments(ctx context.Context, org, classroomID, student string, all bool, enrolled []string, now time.Time) ([]Summary, error)
	listProgress(ctx context.Context, org, classroomID string, enrolled []string) ([]Progress, error)
	// insertSubmission returns errAssessmentChanged when v's questions or marking settings are no longer the stored
	// ones, and errClosed when the assessment no longer takes the submission. late is whether it was stored as late.
	insertSubmission(ctx context.Context, v Assessment, sub Submission) (inserted, late bool, err error)
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

// lockedAssessment is what a change under changeLocked knows of the assessment it locked.
type lockedAssessment struct {
	status, classroom string
}

// changeLocked runs change on the assessment, locked, in one transaction: once nobody has submitted it
// (errHasSubmissions otherwise), unless allowSubmitted. The lock waits for a submission in progress
// (insertSubmission shares it), so the check after it sees that submission, and none starts until it commits.
func (p pgStore) changeLocked(ctx context.Context, org, id string, allowSubmitted bool, change func(tx pgx.Tx, a lockedAssessment) error) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var a lockedAssessment
	if err = tx.QueryRow(ctx, "SELECT status,classroom_id FROM assessments WHERE id=$1 AND org_id=$2 FOR UPDATE", id, org).Scan(&a.status, &a.classroom); err != nil {
		return err // platform.ErrNotFound when it is gone
	}
	if !allowSubmitted {
		var submitted bool
		if err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM submissions WHERE assessment_id=$1)", id).Scan(&submitted); err != nil {
			return err
		}
		if submitted {
			return errHasSubmissions
		}
	}
	if err = change(tx, a); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (p pgStore) updateAssessment(ctx context.Context, org string, before, v Assessment) error {
	data, err := json.Marshal(v.Questions)
	if err != nil {
		return err
	}
	return p.changeLocked(ctx, org, v.ID, false, func(tx pgx.Tx, a lockedAssessment) error {
		current, err := scanAssessment(tx.QueryRow(ctx, "SELECT "+assessmentColumns+" FROM assessments WHERE id=$1", v.ID))
		if err != nil {
			return err
		}
		if !sameMarking(current, before) {
			return errAssessmentChanged // changed since the teacher's version was read, so its kept ids may be stale
		}
		// A published assessment may be replaced, but stays published.
		result, err := tx.Exec(ctx, "UPDATE assessments SET title=$3,questions=$4,status=$5,opens_at=$6,due_at=$7,late_policy=$8,release_policy=$9,ai_marking=$10,published_at=$11 WHERE id=$1 AND org_id=$2 AND (status='draft' OR $5='published')",
			v.ID, org, v.Title, data, v.Status, v.OpensAt, v.DueAt, v.LatePolicy, v.ReleasePolicy, v.AIMarking, v.PublishedAt)
		if err == nil && result.RowsAffected() == 0 {
			err = errNotDraft
		}
		if err != nil || a.status != statusPublished || sameTime(current.DueAt, v.DueAt) {
			return err // nobody has submitted it, so no late flag here changes; a draft's due date nobody else knows
		}
		return emitDueChanged(ctx, tx, org, v.ID, a.classroom, v.DueAt)
	})
}

func (p pgStore) publishDraft(ctx context.Context, org, id string, at time.Time) error {
	result, err := p.db.Exec(ctx, "UPDATE assessments SET status='published',published_at=$3 WHERE id=$1 AND org_id=$2 AND status='draft'", id, org, at)
	if err != nil || result.RowsAffected() == 1 {
		return err
	}
	var status string
	if err = p.db.QueryRow(ctx, "SELECT status FROM assessments WHERE id=$1 AND org_id=$2", id, org).Scan(&status); err != nil {
		return err
	}
	return errNotDraft
}

// updateDetails changes the details under the assessment's lock, so a submission either commits first and is
// judged late again here, or judges itself against the new due date (insertSubmission). A changed due date of a
// published assessment is announced, so Grading's late flags follow it.
func (p pgStore) updateDetails(ctx context.Context, org, id string, d Details) error {
	return p.changeLocked(ctx, org, id, true, func(tx pgx.Tx, a lockedAssessment) error {
		var changed bool
		if err := tx.QueryRow(ctx, "SELECT due_at IS DISTINCT FROM $2::timestamptz FROM assessments WHERE id=$1", id, d.DueAt).Scan(&changed); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "UPDATE assessments SET title=$3,due_at=$4,late_policy=$5 WHERE id=$1 AND org_id=$2", id, org, d.Title, d.DueAt, d.LatePolicy); err != nil {
			return err
		}
		if !changed {
			return nil
		}
		if _, err := tx.Exec(ctx, "UPDATE submissions SET late=coalesce(created_at>$2::timestamptz,false) WHERE assessment_id=$1", id, d.DueAt); err != nil {
			return err
		}
		if a.status != statusPublished {
			return nil // nobody has submitted a draft, and nothing outside Assessment knows of it
		}
		return emitDueChanged(ctx, tx, org, id, a.classroom, d.DueAt)
	})
}

// emitDueChanged announces a published assessment's new due date (nil once removed), so Grading's late flags follow.
func emitDueChanged(ctx context.Context, tx pgx.Tx, org, id, classroom string, due *time.Time) error {
	return platform.Emit(ctx, tx, topicDueChanged, id, map[string]any{"assessment_id": id, "classroom_id": classroom, "org_id": org, "due_at": due})
}

func (p pgStore) deleteAssessment(ctx context.Context, org, id string, submissions bool) error {
	return p.changeLocked(ctx, org, id, submissions, func(tx pgx.Tx, a lockedAssessment) error {
		// The submission events go too, published or not, so no copy of the answers stays in the outbox: those not
		// yet published never are, and the deletion event below follows those already published. Like a classroom
		// purge, this scans the outbox's rows for the topic, which deletions are rare enough to afford. Older versions
		// still in the outbox hold answers too.
		if err := platform.DiscardPendingBy(ctx, tx, topicSubmittedAll, "assessment_id", id, org); err != nil {
			return err
		}
		statements := []string{
			"DELETE FROM saved_answers WHERE assessment_id=$1",
			"DELETE FROM submissions WHERE assessment_id=$1",
			"DELETE FROM assessments WHERE id=$1",
		}
		if err := platform.ExecAll(ctx, tx, statements, id); err != nil {
			return err
		}
		if a.status != statusPublished {
			return nil // nobody but the teacher has seen a draft, so nothing outside Assessment knows of it
		}
		return platform.Emit(ctx, tx, topicDeleted, id, map[string]string{"assessment_id": id, "classroom_id": a.classroom, "org_id": org})
	})
}

// listProgress is the classroom's 100 newest assessments with how many of the enrolled students submitted each.
func (p pgStore) listProgress(ctx context.Context, org, classroomID string, enrolled []string) ([]Progress, error) {
	rows, err := p.db.Query(ctx, "SELECT a.id,a.title,count(s.id),a.status,a.due_at FROM assessments a LEFT JOIN submissions s ON s.assessment_id=a.id AND s.student_id=ANY($3::text[]) WHERE a.classroom_id=$1 AND a.org_id=$2 GROUP BY a.id ORDER BY a.created_at DESC LIMIT 100", classroomID, org, enrolled)
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
// all, only those published and open; with it, for the teacher, each with how many of the enrolled students have
// submitted it and whether anyone has, removed students included (which is what stops a replacement or a plain
// deletion). The question count and points are summed in SQL, so the questions (answer keys and rubrics included) are
// never read for a list; a question stored without points is worth one, as in decodeQuestions.
func (p pgStore) listAssessments(ctx context.Context, org, classroomID, student string, all bool, enrolled []string, now time.Time) ([]Summary, error) {
	rows, err := p.db.Query(ctx, `SELECT a.id,a.title,EXISTS(SELECT 1 FROM submissions s WHERE s.assessment_id=a.id AND s.student_id=$3),
		a.status,a.opens_at,a.due_at,a.late_policy,jsonb_array_length(a.questions),
		(SELECT coalesce(sum(coalesce(nullif((q->>'points')::int,0),1)),0) FROM jsonb_array_elements(a.questions) q),
		CASE WHEN $4 THEN (SELECT count(*) FROM submissions s WHERE s.assessment_id=a.id AND s.student_id=ANY($6::text[])) END,
		CASE WHEN $4 THEN EXISTS(SELECT 1 FROM submissions s WHERE s.assessment_id=a.id) END
		FROM assessments a WHERE a.classroom_id=$1 AND a.org_id=$2 AND ($4 OR (a.status='published' AND (a.opens_at IS NULL OR a.opens_at<=$5)))
		ORDER BY a.created_at DESC LIMIT 100`, classroomID, org, student, all, now, enrolled)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Summary, error) {
		var v Summary
		err := row.Scan(&v.ID, &v.Title, &v.Submitted, &v.Status, &v.OpensAt, &v.DueAt, &v.LatePolicy, &v.Questions, &v.Points, &v.Submissions, &v.HasSubmissions)
		return v, err
	})
}

// insertSubmission stores the submission, removes the student's saved work and emits the outbox event in one
// transaction. It reports inserted=false (with a nil error) when the student has already submitted this
// assessment, and with late whether the submission was stored as late.
func (p pgStore) insertSubmission(ctx context.Context, v Assessment, sub Submission) (bool, bool, error) {
	answers, err := json.Marshal(sub.Answers)
	if err != nil {
		return false, false, err
	}
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return false, false, err
	}
	defer tx.Rollback(ctx)
	if err = lockStudentWork(ctx, tx, v.ID, sub.Student); err != nil {
		return false, false, err
	}
	// Shared with other submissions, exclusive against a teacher's change (changeLocked): the answers are kept
	// only if they are to the questions as they are now.
	current, err := scanAssessment(tx.QueryRow(ctx, "SELECT "+assessmentColumns+" FROM assessments WHERE id=$1 FOR SHARE", v.ID))
	if err != nil {
		return false, false, err
	}
	if !sameMarking(current, v) {
		return false, false, errAssessmentChanged
	}
	// Against the due date and late policy as they are now, should the teacher have just changed them.
	if current.closed(sub.SubmittedAt) {
		return false, false, errClosed
	}
	sub.Late = current.late(sub.SubmittedAt)
	result, err := tx.Exec(ctx, "INSERT INTO submissions(id,assessment_id,student_id,answers,late,created_at) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(assessment_id,student_id) DO NOTHING", sub.ID, v.ID, sub.Student, answers, sub.Late, sub.SubmittedAt)
	if err != nil {
		return false, false, err
	}
	if result.RowsAffected() == 0 {
		return false, false, nil
	}
	if _, err = tx.Exec(ctx, "DELETE FROM saved_answers WHERE assessment_id=$1 AND student_id=$2", v.ID, sub.Student); err != nil {
		return false, false, err
	}
	err = platform.Emit(ctx, tx, topicSubmitted, sub.ID, submittedEvent(v, sub))
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		return false, false, err
	}
	return true, sub.Late, nil
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

// errAccountDeleted is the answer to a submission or save by a student whose account deletion is being purged.
var errAccountDeleted = errors.New("the account was deleted")

// lockStudentWork serializes one student's saving and submitting of one assessment until tx ends, so saved work
// cannot be written after the submission that deletes it. It also holds the student shared against the purge of a
// deleted account (purgeUser), and returns errAccountDeleted once that has run.
func lockStudentWork(ctx context.Context, tx pgx.Tx, assessment, student string) error {
	if err := platform.Lock(ctx, tx, platform.LockStudentWork, assessment+"/"+student); err != nil {
		return err
	}
	if err := platform.LockShared(ctx, tx, platform.LockUser, student); err != nil {
		return err
	}
	var deleted bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM deleted_users WHERE user_id=$1)", student).Scan(&deleted); err != nil {
		return err
	}
	if deleted {
		return errAccountDeleted
	}
	return nil
}

// saveAnswers replaces the student's saved work, unless they have already submitted (errAlreadySubmitted) or the
// assessment is gone (platform.ErrNotFound).
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
	// Shared like a submission's, so a teacher deleting the assessment waits for this save or this save finds it gone.
	var exists int
	if err = tx.QueryRow(ctx, "SELECT 1 FROM assessments WHERE id=$1 FOR SHARE", assessment).Scan(&exists); err != nil {
		return time.Time{}, err // platform.ErrNotFound when it is gone
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
