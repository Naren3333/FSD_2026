package main

import (
	"context"
	"encoding/json"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// purge classroom grades and jobs; remember deletion to reject late events.
func purge(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeClassroomDeleted(event)
	if err != nil {
		return err
	}
	statements := []string{
		"INSERT INTO purged_classrooms(classroom_id,org_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
		"DELETE FROM removed_enrollments WHERE classroom_id=$1 AND org_id=$2",
		"DELETE FROM assessment_due WHERE classroom_id=$1 AND org_id=$2",
		// locked first, so a teacher's change holding one (with its audit entry) commits before the audit is
		// read.
		"SELECT 1 FROM evaluations WHERE classroom_id=$1 AND org_id=$2 FOR UPDATE",
		"DELETE FROM marking_jobs WHERE evaluation_id IN (SELECT id FROM evaluations WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM audit WHERE evaluation_id IN (SELECT id FROM evaluations WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM evaluations WHERE classroom_id=$1 AND org_id=$2",
	}
	if err = platform.ExecAll(ctx, tx, statements, v.Classroom, v.Org); err != nil {
		return err
	}
	return platform.DiscardPending(ctx, tx, v)
}

// topicassessmentdeleted is assessment's event for a published assessment its teacher deleted with its
// submissions (adr 0008).
const topicAssessmentDeleted = "assessment.deleted.v1"

// assessmentdeleted is the topicassessmentdeleted event's data.
type AssessmentDeleted struct {
	Assessment string `json:"assessment_id"`
	Classroom  string `json:"classroom_id"`
	Org        string `json:"org_id"`
}

// purge assessment grades and jobs; remember deletion to reject late events.
func purgeAssessment(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	var v AssessmentDeleted
	if json.Unmarshal(event.Data, &v) != nil || !platform.ID(v.Assessment) || !platform.ID(v.Classroom) || v.Org == "" || event.Resource != v.Assessment {
		return platform.InvalidEvent{Reason: "invalid assessment deletion"}
	}
	if err := platform.Lock(ctx, tx, platform.LockAssessment, v.Assessment); err != nil {
		return err
	}
	statements := []string{
		"INSERT INTO deleted_assessments(assessment_id,classroom_id,org_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
		"DELETE FROM assessment_due WHERE assessment_id=$1 AND classroom_id=$2 AND org_id=$3",
		"SELECT 1 FROM evaluations WHERE assessment_id=$1 AND classroom_id=$2 AND org_id=$3 FOR UPDATE", // as in purge
		"DELETE FROM marking_jobs WHERE evaluation_id IN (SELECT id FROM evaluations WHERE assessment_id=$1 AND classroom_id=$2 AND org_id=$3)",
		"DELETE FROM audit WHERE evaluation_id IN (SELECT id FROM evaluations WHERE assessment_id=$1 AND classroom_id=$2 AND org_id=$3)",
		"DELETE FROM evaluations WHERE assessment_id=$1 AND classroom_id=$2 AND org_id=$3",
	}
	if err := platform.ExecAll(ctx, tx, statements, v.Assessment, v.Classroom, v.Org); err != nil {
		return err
	}
	return platform.DiscardPendingBy(ctx, tx, "%", "assessment_id", v.Assessment, v.Org)
}

// purge deleted-student grades and jobs; remember deletion to reject late events.
func purgeUser(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeUserDeleted(event)
	if err != nil {
		return err
	}
	if err = platform.Lock(ctx, tx, platform.LockUser, v.User); err != nil {
		return err
	}
	statements := []string{
		"INSERT INTO deleted_users(user_id,org_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
		"DELETE FROM removed_enrollments WHERE student_id=$1 AND org_id=$2",
		"SELECT 1 FROM evaluations WHERE student_id=$1 AND org_id=$2 FOR UPDATE", // as in purge
		"DELETE FROM marking_jobs WHERE evaluation_id IN (SELECT id FROM evaluations WHERE student_id=$1 AND org_id=$2)",
		"DELETE FROM audit WHERE evaluation_id IN (SELECT id FROM evaluations WHERE student_id=$1 AND org_id=$2)",
		"DELETE FROM evaluations WHERE student_id=$1 AND org_id=$2",
	}
	if err = platform.ExecAll(ctx, tx, statements, v.User, v.Org); err != nil {
		return err
	}
	return platform.DiscardPendingBy(ctx, tx, "%", "student_id", v.User, v.Org)
}
