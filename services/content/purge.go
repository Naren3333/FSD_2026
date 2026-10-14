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

// folders is what purging needs from object storage.
type folders interface {
	removeAll(ctx context.Context, prefix string) error
}

// purger removes a deleted classroom's documents: their rows (the chunks, vectors and indexing jobs go with them),
// their originals and figure crops in object storage, and any of their events not yet published.
type purger struct{ files folders }

// purge runs in the consumer's transaction. The objects are removed before the transaction commits, so a failure
// leaves the rows in place and the retry removes whatever is left; deleting what is already gone changes nothing,
// so a repeated event is harmless.
func (p purger) purge(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	var v classroomDeleted
	if json.Unmarshal(event.Data, &v) != nil || !platform.ID(v.Classroom) || v.Org == "" || event.Resource != v.Classroom {
		return platform.InvalidEvent{Reason: "invalid classroom deletion"}
	}
	rows, err := tx.Query(ctx, "DELETE FROM documents WHERE classroom_id=$1 AND org_id=$2 RETURNING id::text", v.Classroom, v.Org)
	if err != nil {
		return err
	}
	documents, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "DELETE FROM outbox WHERE body->'data'->>'classroom_id'=$1::text AND body->'data'->>'org_id'=$2", v.Classroom, v.Org); err != nil {
		return err
	}
	for _, document := range documents {
		if err = p.files.removeAll(ctx, v.Org+"/"+document+"/"); err != nil {
			return err
		}
	}
	return nil
}
