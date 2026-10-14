package main

import (
	"context"
	"encoding/json"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// classroomDeleted is Identity's identity.classroom.deleted.v1 event.
type classroomDeleted struct {
	Classroom string `json:"classroom_id"`
	Org       string `json:"org_id"`
}

// purge removes a deleted classroom's evaluations, their audit trail and any of its grading events not yet
// published, and remembers the classroom so a late submission event cannot grade it again. Deleting what is
// already gone changes nothing, so a repeated event is harmless.
func purge(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	var v classroomDeleted
	if json.Unmarshal(event.Data, &v) != nil || !platform.ID(v.Classroom) || v.Org == "" || event.Resource != v.Classroom {
		return platform.InvalidEvent{Reason: "invalid classroom deletion"}
	}
	statements := []string{
		"INSERT INTO purged_classrooms(classroom_id,org_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
		"DELETE FROM audit WHERE evaluation_id IN (SELECT id FROM evaluations WHERE classroom_id=$1 AND org_id=$2)",
		"DELETE FROM evaluations WHERE classroom_id=$1 AND org_id=$2",
		"DELETE FROM outbox WHERE body->'data'->>'classroom_id'=$1::text AND body->'data'->>'org_id'=$2",
	}
	for _, sql := range statements {
		if _, err := tx.Exec(ctx, sql, v.Classroom, v.Org); err != nil {
			return err
		}
	}
	return nil
}
