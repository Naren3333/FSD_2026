package main

import (
	"context"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// purge removes a deleted classroom's assessments with their submissions and saved work, with any of its submission events not yet
// published. Deleting what is already gone changes nothing, so a repeated event is harmless.
func purge(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeClassroomDeleted(event)
	if err != nil {
		return err
	}
	statements := []string{
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
