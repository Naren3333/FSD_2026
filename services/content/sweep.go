package main

import (
	"context"
	"learning/platform"
	"log/slog"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// sweepgrace is how old an object must be before the sweeper may remove it: longer than any upload
// (uploaddeadline) or indexing run (ingesttimeout) that is still writing objects its row will name.
const sweepGrace = 6 * time.Hour

// sweepevery is how often each replica sweeps; replicas sweeping at once only remove the same objects
// twice.
const sweepEvery = time.Hour

// storedobject is an object in the bucket with when it was last written.
type storedObject struct {
	key      string
	modified time.Time
}

// bucket is what sweeping needs from object storage.
type bucket interface {
	list(ctx context.Context) ([]storedObject, error)
	// modified is when the object was last written, with exists false once it is gone.
	modified(ctx context.Context, key string) (at time.Time, exists bool, err error)
	remove(ctx context.Context, key string) error
}

// remove unreferenced originals and crops left behind by failed writes, purges or reindexing.
type sweeper struct {
	db    *pgxpool.Pool
	files bucket
}

// run sweeps every sweepevery until ctx ends.
func (s sweeper) run(ctx context.Context) {
	ticker := time.NewTicker(sweepEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		removed, err := s.sweep(ctx, time.Now().Add(-sweepGrace))
		if err != nil {
			slog.Error("sweep_failed", "removed", removed, "error", err.Error())
		} else if removed > 0 {
			slog.Info("sweep_removed_objects", "removed", removed)
		}
	}
}

// owner is what a document row says about the objects under its prefix.
type owner struct {
	original string
	indexing bool
	figures  []string // the crops its chunks cite
}

// sweep removes every object written before cutoff that no document row names, and reports how many it
// removed.
func (s sweeper) sweep(ctx context.Context, cutoff time.Time) (int, error) {
	objects, err := s.files.list(ctx)
	if err != nil {
		return 0, err
	}
	old := writtenBefore(objects, cutoff)
	if len(old) == 0 {
		return 0, nil
	}
	ids := make([]string, 0, len(old))
	for id := range old {
		ids = append(ids, id)
	}
	owners, err := readOwners(ctx, s.db, ids)
	if err != nil {
		return 0, err
	}
	return s.removeUnowned(ctx, old, owners, cutoff)
}

// recheck candidate objects under their document's lock before removal.
func (s sweeper) removeUnowned(ctx context.Context, old map[string][]string, owners map[string]owner, cutoff time.Time) (int, error) {
	removed := 0
	for id, keys := range old {
		o, owned := owners[id]
		candidates := slices.DeleteFunc(slices.Clone(keys), func(key string) bool { return owned && o.keeps(key) })
		if len(candidates) == 0 {
			continue
		}
		n, err := s.removeLocked(ctx, id, candidates, cutoff)
		removed += n
		if err != nil {
			return removed, err
		}
	}
	return removed, nil
}

// removelocked removes the document's keys its row does not keep, holding the row for share from reading it
// until the last removal, and reports how many it removed.
func (s sweeper) removeLocked(ctx context.Context, id string, keys []string, cutoff time.Time) (int, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT 1 FROM documents WHERE id=$1 FOR SHARE", id); err != nil {
		return 0, err
	}
	owners, err := readOwners(ctx, tx, []string{id})
	if err != nil {
		return 0, err
	}
	o, owned := owners[id]
	removed := 0
	for _, key := range keys {
		if owned && o.keeps(key) {
			continue
		}
		at, exists, err := s.files.modified(ctx, key)
		if err != nil {
			return removed, err
		}
		if !exists || !at.Before(cutoff) {
			continue
		}
		if err := s.files.remove(ctx, key); err != nil {
			return removed, err
		}
		removed++
	}
	return removed, tx.Commit(ctx)
}

// writtenbefore groups the keys of objects written before cutoff by the document id in their path.
func writtenBefore(objects []storedObject, cutoff time.Time) map[string][]string {
	old := map[string][]string{}
	for _, o := range objects {
		parts := strings.SplitN(o.key, "/", 3)
		if !o.modified.Before(cutoff) || len(parts) != 3 || !platform.ID(parts[1]) {
			continue
		}
		old[parts[1]] = append(old[parts[1]], o.key)
	}
	return old
}

// keeps says whether the row still needs the object: all of them while it indexes, else its original and
// cited crops.
func (o owner) keeps(key string) bool {
	return o.indexing || key == o.original || slices.Contains(o.figures, key)
}

// querier is a pool or a transaction.
type querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

// readowners reads the rows of the documents named, keyed by id: each one's original and the figure crops
// its chunks cite, as object keys.
func readOwners(ctx context.Context, db querier, ids []string) (map[string]owner, error) {
	rows, err := db.Query(ctx, `SELECT d.id::text,d.object_key,d.status='indexing',
		coalesce(array_agg(d.org_id||'/'||d.id||'/figures/'||c.image) FILTER (WHERE c.image IS NOT NULL),'{}')
		FROM documents d LEFT JOIN document_chunks c ON c.document_id=d.id WHERE d.id=ANY($1::uuid[]) GROUP BY d.id`, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]owner{}
	for rows.Next() {
		var id string
		var o owner
		if err = rows.Scan(&id, &o.original, &o.indexing, &o.figures); err != nil {
			return nil, err
		}
		result[id] = o
	}
	return result, rows.Err()
}
