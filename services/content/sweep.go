package main

import (
	"context"
	"learning/platform"
	"log/slog"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// sweepGrace is how old an object must be before the sweeper may remove it: longer than any upload
// (uploadDeadline) or indexing run (ingestTimeout) that is still writing objects its row will name.
const sweepGrace = 6 * time.Hour

// sweepEvery is how often each replica sweeps; replicas sweeping at once only remove the same objects twice.
const sweepEvery = time.Hour

// storedObject is an object in the bucket with when it was last written.
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

// sweeper removes objects no document row names, which nothing could otherwise reach or delete: an original
// whose upload never committed and whose cleanup failed (removeOrphan), what a delete or purge cut short left, and
// figure crops an earlier index stored that a reindex no longer cites.
type sweeper struct {
	db    *pgxpool.Pool
	files bucket
}

// run sweeps every sweepEvery until ctx ends.
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

// sweep removes every object written before cutoff that no document row names, and reports how many it removed.
// Objects of a document being indexed are left alone: the run may still be storing them.
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
	owners, err := s.owners(ctx, ids)
	if err != nil {
		return 0, err
	}
	return s.removeUnowned(ctx, old, owners, cutoff)
}

// removeUnowned removes the keys, grouped by document id, that their document's row does not keep, and reports how
// many it removed before any failure. Each is looked at again just before it goes: a reindex that started after the
// rows were read may have stored the same crop again (a crop's key is its content's hash), and one written since
// cutoff is left for a later sweep to judge.
func (s sweeper) removeUnowned(ctx context.Context, old map[string][]string, owners map[string]owner, cutoff time.Time) (int, error) {
	removed := 0
	for id, keys := range old {
		o, owned := owners[id]
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
	}
	return removed, nil
}

// writtenBefore groups the keys of objects written before cutoff by the document id in their path.
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

// keeps says whether the row still needs the object: all of them while it indexes, else its original and cited crops.
func (o owner) keeps(key string) bool {
	return o.indexing || key == o.original || slices.Contains(o.figures, key)
}

// owners reads the rows of the documents named, keyed by id: each one's original and the figure crops its chunks
// cite, as object keys.
func (s sweeper) owners(ctx context.Context, ids []string) (map[string]owner, error) {
	rows, err := s.db.Query(ctx, `SELECT d.id::text,d.object_key,d.status='indexing',
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
