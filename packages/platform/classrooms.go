package platform

import (
	"context"
	"encoding/json"

	"github.com/jackc/pgx/v5"
)

// TopicClassroomDeleted is Identity's event announcing a deleted classroom: every service that keeps data for the
// classroom purges it.
const TopicClassroomDeleted = "identity.classroom.deleted.v1"

// ClassroomDeleted is the TopicClassroomDeleted event's data.
type ClassroomDeleted struct {
	Classroom string `json:"classroom_id"`
	Org       string `json:"org_id"`
}

// DecodeClassroomDeleted reads a classroom deletion. A malformed one, or one whose resource is not its classroom,
// is an InvalidEvent, so it is dead-lettered instead of retried.
func DecodeClassroomDeleted(event Event) (ClassroomDeleted, error) {
	var v ClassroomDeleted
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.Classroom) || v.Org == "" || event.Resource != v.Classroom {
		return ClassroomDeleted{}, InvalidEvent{Reason: "invalid classroom deletion"}
	}
	return v, nil
}

// DiscardPending removes, inside tx, the outbox rows of events about the deleted classroom, so those not yet
// published never are. The service's own data for the classroom is the caller's to delete in the same transaction.
func DiscardPending(ctx context.Context, tx pgx.Tx, v ClassroomDeleted) error {
	_, err := tx.Exec(ctx, "DELETE FROM outbox WHERE body->'data'->>'classroom_id'=$1::text AND body->'data'->>'org_id'=$2", v.Classroom, v.Org)
	return err
}
