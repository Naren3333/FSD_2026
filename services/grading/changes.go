package main

import (
	"context"
	"encoding/json"
	"learning/platform"
	"time"

	"github.com/jackc/pgx/v5"
)

// removestudent records that a teacher removed a student from a classroom
// (platform.topicenrollmentremoved), at the time identity removed them.
func removeStudent(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeEnrollmentRemoved(event)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO removed_enrollments(classroom_id,student_id,org_id,removed_at) VALUES($1,$2,$3,$4)
		ON CONFLICT(classroom_id,student_id) DO UPDATE SET removed_at=GREATEST(removed_enrollments.removed_at,EXCLUDED.removed_at)`,
		v.Classroom, v.Student, v.Org, event.Timestamp)
	return err
}

// topicduechanged is assessment's event for a published assessment whose due date its teacher changed
// (contracts/events/assessment.due_changed.v1.schema.json).
const topicDueChanged = "assessment.due_changed.v1"

// duechanged is the topicduechanged event's data; dueat is nil when the due date was removed.
type DueChanged struct {
	Assessment string     `json:"assessment_id"`
	Classroom  string     `json:"classroom_id"`
	Org        string     `json:"org_id"`
	DueAt      *time.Time `json:"due_at"`
}

// changedue keeps the assessment's latest due date and sets each of its evaluations' late flag against it:
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
