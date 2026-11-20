package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"time"

	"github.com/jackc/pgx/v5"
)

// removeStudent records that a student was removed from a classroom
// (platform.TopicEnrollmentRemoved), at the time identity removed them.
func removeStudent(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeEnrollmentRemoved(event)
	if err != nil {
		return err
	}
	if err = lockEnrollment(ctx, tx, v.Classroom, v.Student); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO removed_enrollments(classroom_id,student_id,org_id,removed_at)
		SELECT $1,$2,$3,$4 WHERE NOT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$1)
			AND NOT EXISTS(SELECT 1 FROM deleted_users WHERE user_id=$2)
		ON CONFLICT(classroom_id,student_id) DO UPDATE SET removed_at=GREATEST(removed_enrollments.removed_at,EXCLUDED.removed_at)`,
		v.Classroom, v.Student, v.Org, event.Timestamp)
	return err
}

// lockEnrollment takes, shared and in a submission's order, the locks the purges of the student's account and
// of the classroom take exclusively, so an enrollment change consumed after either records nothing.
func lockEnrollment(ctx context.Context, tx pgx.Tx, classroom, student string) error {
	if err := platform.LockShared(ctx, tx, platform.LockUser, student); err != nil {
		return err
	}
	return platform.LockShared(ctx, tx, platform.LockClassroom, classroom)
}

// restoreStudent records the newest time a student was enrolled in a classroom (platform.TopicEnrollmentAdded,
// in enrollment_returns): work they submitted before a removal is marked again once a return is newer than
// it. an enrollment may arrive before an older removal, so it is kept whether or not it brings them back now.
func restoreStudent(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeEnrollmentAdded(event)
	if err != nil {
		return err
	}
	if err = lockEnrollment(ctx, tx, v.Classroom, v.Student); err != nil {
		return err
	}
	// only a newer return is recorded, and only one after the student's latest removal brings them back: a
	// first enrollment, with no removal, has nothing to queue again.
	var back bool
	err = tx.QueryRow(ctx, `INSERT INTO enrollment_returns(classroom_id,student_id,org_id,restored_at)
		SELECT $1,$2,$3,$4 WHERE NOT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$1)
			AND NOT EXISTS(SELECT 1 FROM deleted_users WHERE user_id=$2)
		ON CONFLICT(classroom_id,student_id) DO UPDATE SET restored_at=EXCLUDED.restored_at WHERE enrollment_returns.restored_at<EXCLUDED.restored_at
		RETURNING EXISTS(SELECT 1 FROM removed_enrollments WHERE classroom_id=$1 AND student_id=$2 AND removed_at<$4)`,
		v.Classroom, v.Student, v.Org, event.Timestamp).Scan(&back)
	if errors.Is(err, pgx.ErrNoRows) || err == nil && !back {
		return nil
	}
	if err != nil {
		return err
	}
	return requeueRemoved(ctx, tx, v)
}

// indexes are what grading builds beside its work (platform.IndexBuilder): the outbox's prune and purge
// indexes; the one requeueRemoved finds a student's drafts in a classroom through; and those an account's purge
// finds the student's evaluations through, and every purge their audit entries, for the delete and the check
// of its foreign key.
func indexes() []platform.Index {
	outbox := append(platform.OutboxIndexes(), platform.PurgeIndexes(platform.PurgeByAssessment, platform.PurgeByClassroom, platform.PurgeByStudent)...)
	return append(outbox,
		platform.Index{Name: "evaluations_classroom_student", Table: "evaluations", Definition: "(classroom_id, student_id)"},
		platform.Index{Name: "evaluations_student", Table: "evaluations", Definition: "(student_id)"},
		platform.Index{Name: "audit_evaluation", Table: "audit", Definition: "(evaluation_id)"})
}

// draftsOfStudent locks a student's drafts in a classroom, in id order.
const draftsOfStudent = "SELECT id,evidence FROM evaluations WHERE classroom_id=$1 AND student_id=$2 AND org_id=$3 AND status=$4 ORDER BY id FOR UPDATE"

// requeueRemoved queues again, pending, the marking of the student's drafts in the classroom that was settled
// while they were away (STUDENT_REMOVED): the marker checks their enrollment again before any ai call, so a
// return older than their removal settles it the same way without cost. every draft is locked, as the
// marker's settlement locks one, so a settlement under way either commits first and is queued again here or
// waits and sees this return (settle).
func requeueRemoved(ctx context.Context, tx pgx.Tx, v platform.EnrollmentAdded) error {
	rows, err := tx.Query(ctx, draftsOfStudent, v.Classroom, v.Student, v.Org, statusDraft)
	if err != nil {
		return err
	}
	type draft struct {
		id       string
		evidence []byte
	}
	drafts, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (draft, error) {
		var d draft
		return d, row.Scan(&d.id, &d.evidence)
	})
	if err != nil {
		return err
	}
	// each job's evaluation and question, and each requeued draft with its new evidence.
	var jobEvaluations, jobQuestions, requeued, evidences []string
	for _, d := range drafts {
		var evidence []Evidence
		if err = json.Unmarshal(d.evidence, &evidence); err != nil {
			return err
		}
		changed := false
		for i, e := range evidence {
			if e.Suggestion == nil || e.Suggestion.Reason != reasonStudentRemoved {
				continue
			}
			evidence[i].Suggestion = &Suggestion{Status: suggestionPending}
			jobEvaluations, jobQuestions, changed = append(jobEvaluations, d.id), append(jobQuestions, e.Question), true
		}
		if !changed {
			continue
		}
		data, err := json.Marshal(evidence)
		if err != nil {
			return err
		}
		requeued, evidences = append(requeued, d.id), append(evidences, string(data))
	}
	if len(requeued) == 0 {
		return nil
	}
	// one statement each, however many drafts the student has, so the drafts' locks are held briefly.
	if _, err = tx.Exec(ctx, "INSERT INTO marking_jobs(evaluation_id,question_id) SELECT * FROM unnest($1::uuid[],$2::uuid[]) ON CONFLICT DO NOTHING", jobEvaluations, jobQuestions); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, "UPDATE evaluations e SET evidence=v.evidence::jsonb FROM unnest($1::uuid[],$2::text[]) AS v(id,evidence) WHERE e.id=v.id", requeued, evidences)
	return err
}

// topicDueChanged is assessment's event for a published assessment whose due date its teacher changed
// (contracts/events/assessment.due_changed.v1.schema.json).
const topicDueChanged = "assessment.due_changed.v1"

// DueChanged is the topicDueChanged event's data; DueAt is nil when the due date was removed.
type DueChanged struct {
	Assessment string     `json:"assessment_id"`
	Classroom  string     `json:"classroom_id"`
	Org        string     `json:"org_id"`
	DueAt      *time.Time `json:"due_at"`
}

// changeDue keeps the assessment's latest due date and sets each of its evaluations' late flag against it:
// late only when submitted after it, never when there is none.
func changeDue(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	var v DueChanged
	if json.Unmarshal(event.Data, &v) != nil || !platform.ID(v.Assessment) || !platform.ID(v.Classroom) || v.Org == "" || event.Resource != v.Assessment {
		return platform.InvalidEvent{Reason: "invalid due date change"}
	}
	if err := platform.Lock(ctx, tx, platform.LockAssessment, v.Assessment); err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `INSERT INTO assessment_due(assessment_id,classroom_id,org_id,due_at,changed_at)
		SELECT $1,$2,$3,$4,$5 WHERE NOT EXISTS(SELECT 1 FROM deleted_assessments WHERE assessment_id=$1) AND NOT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$2)
		ON CONFLICT(assessment_id) DO UPDATE SET due_at=EXCLUDED.due_at,changed_at=EXCLUDED.changed_at WHERE assessment_due.changed_at<EXCLUDED.changed_at`,
		v.Assessment, v.Classroom, v.Org, v.DueAt, event.Timestamp)
	if err != nil || tag.RowsAffected() == 0 {
		return err
	}
	_, err = tx.Exec(ctx, "UPDATE evaluations SET late=coalesce(submitted_at>$3,false) WHERE assessment_id=$1 AND org_id=$2 AND submitted_at IS NOT NULL", v.Assessment, v.Org, v.DueAt)
	return err
}
