package main

import (
	"context"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// purge removes a deleted classroom's assessments with their submissions and saved work, with any of its submission events not yet
// published, and remembers the classroom so an assessment created meanwhile is refused. Deleting what is already gone
// changes nothing, so a repeated event is harmless.
func purge(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeClassroomDeleted(event)
	if err != nil {
		return err
	}
	// Waits for assessments being created in the classroom, and makes later ones see it purged.
	if err = platform.Lock(ctx, tx, platform.LockClassroom, v.Classroom); err != nil {
		return err
	}
	statements := []string{
		"INSERT INTO purged_classrooms(classroom_id,org_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
		"DELETE FROM saved_answers WHERE assessment_id IN (SELECT id FROM assessments WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM submissions WHERE assessment_id IN (SELECT id FROM assessments WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM assessments WHERE classroom_id=$1 AND org_id=$2",
	}
	for _, sql := range statements {
		if _, err = tx.Exec(ctx, sql, v.Classroom, v.Org); err != nil {
			return err
		}
	}
	return platform.DiscardPending(ctx, tx, v)
}

// purgeUser removes the submissions and saved work of a student who deleted their account, and their submission
// events not yet published, and remembers them so a submission or save racing the deletion is refused. It holds
// the student's lock against lockStudentWork. A teacher deletes their classrooms first, and those purges remove
// their assessments, so a teacher's deletion leaves nothing here. A repeated event changes nothing.
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
		// Published or not, as for a deleted assessment: no copy of the student's answers stays in the outbox.
		"DELETE FROM outbox WHERE topic LIKE '" + topicSubmittedAll + "' AND body->'data'->>'student_id'=$1::text AND body->'data'->>'org_id'=$2",
	}
	for _, sql := range statements {
		if _, err = tx.Exec(ctx, sql, v.User, v.Org); err != nil {
			return err
		}
	}
	return nil
}
