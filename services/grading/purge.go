package main

import (
	"context"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// purge removes a deleted classroom's evaluations, their audit trail and marking jobs, and any of its grading events not yet
// published, and remembers the classroom so a late submission event cannot grade it again. Deleting what is
// already gone changes nothing, so a repeated event is harmless.
func purge(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeClassroomDeleted(event)
	if err != nil {
		return err
	}
	statements := []string{
		"INSERT INTO purged_classrooms(classroom_id,org_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
		"DELETE FROM marking_jobs WHERE evaluation_id IN (SELECT id FROM evaluations WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM audit WHERE evaluation_id IN (SELECT id FROM evaluations WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM evaluations WHERE classroom_id=$1 AND org_id=$2",
	}
	for _, sql := range statements {
		if _, err = tx.Exec(ctx, sql, v.Classroom, v.Org); err != nil {
			return err
		}
	}
	return platform.DiscardPending(ctx, tx, v)
}
