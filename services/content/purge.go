package main

import (
	"context"
	"learning/platform"
	"time"

	"github.com/jackc/pgx/v5"
)

// purgeTimeout bounds deleting a large classroom's rows and recording durable object cleanup jobs.
const purgeTimeout = 5 * time.Minute

// folders is what purging needs from object storage.
type folders interface {
	removeAll(ctx context.Context, prefix string) error
}

// purger removes a deleted classroom's documents: their rows (the chunks, vectors and indexing jobs go with them),
// their originals and figure crops in object storage, and any of their events not yet published. It records the
// classroom as purged, so an upload still under way is refused.
type purger struct{}

// purge runs in the consumer's transaction. Durable cleanup jobs commit with the rows' deletion and inbox;
// a duplicate event cannot lose cleanup work or remove objects before a successful commit.
func (p purger) purge(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeClassroomDeleted(event)
	if err != nil {
		return err
	}
	// Waits for uploads into the classroom that are committing, and makes later ones see it purged.
	if err = platform.Lock(ctx, tx, platform.LockClassroom, v.Classroom); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "INSERT INTO purged_classrooms(classroom_id,org_id) VALUES($1,$2) ON CONFLICT DO NOTHING", v.Classroom, v.Org); err != nil {
		return err
	}
	rows, err := tx.Query(ctx, "DELETE FROM documents WHERE classroom_id=$1 AND org_id=$2 RETURNING id::text", v.Classroom, v.Org)
	if err != nil {
		return err
	}
	documents, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return err
	}
	if err = platform.DiscardPending(ctx, tx, v); err != nil {
		return err
	}
	for _, document := range documents {
		if _, err = tx.Exec(ctx, "INSERT INTO object_cleanup_jobs(prefix) VALUES($1) ON CONFLICT DO NOTHING", v.Org+"/"+document+"/"); err != nil {
			return err
		}
	}
	return nil
}
