package main

import (
	"context"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// purge classroom work and pending events; remember deletion to reject racing writes.
func purge(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeClassroomDeleted(event)
	if err != nil {
		return err
	}
	// waits for assessments being created in the classroom, and makes later ones see it purged.
	if err = platform.Lock(ctx, tx, platform.LockClassroom, v.Classroom); err != nil {
		return err
	}
	statements := []string{
		"INSERT INTO purged_classrooms(classroom_id,org_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
		"DELETE FROM saved_answers WHERE assessment_id IN (SELECT id FROM assessments WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM submissions WHERE assessment_id IN (SELECT id FROM assessments WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM assessments WHERE classroom_id=$1 AND org_id=$2",
	}
	if err = platform.ExecAll(ctx, tx, statements, v.Classroom, v.Org); err != nil {
		return err
	}
	return platform.DiscardPending(ctx, tx, v)
}

// purge a deleted student's work and pending events; reject racing writes.
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
		"DELETE FROM saved_answers WHERE student_id=$1 AND assessment_id IN (SELECT id FROM assessments WHERE org_id=$2)",
		"DELETE FROM submissions WHERE student_id=$1 AND assessment_id IN (SELECT id FROM assessments WHERE org_id=$2)",
	}
	if err = platform.ExecAll(ctx, tx, statements, v.User, v.Org); err != nil {
		return err
	}
	// published or not, as for a deleted assessment: no copy of the student's answers stays in the outbox.
	return platform.DiscardPendingBy(ctx, tx, topicSubmittedAll, "student_id", v.User, v.Org)
}
