package platform

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Index is an index a service builds concurrently beside its work (IndexBuilder) rather than in a migration,
// so the build never holds its table's writes. its fields are the service's own sql, never input.
type Index struct {
	Name, Table string
	Definition  string // what follows ON table: the columns or expressions in parentheses, and any WHERE
}

// errIndexesBusy is BuildIndexes' answer while another session, such as another replica, holds the build.
var errIndexesBusy = errors.New("another session is building the indexes")

// how long IndexBuilder first waits after a failed or busy build, doubling up to indexRetryMax. tests shorten
// it.
var (
	indexRetry    = 5 * time.Second
	indexRetryMax = 5 * time.Minute
)

// BuildIndexes creates the indexes concurrently, in order, skipping those already built. one left invalid by
// an interrupted build is dropped and built again. each is looked up on its own table, so a same-named index
// elsewhere on the search path is never mistaken for it. only one session of the schema builds at a time:
// while another does, it returns errIndexesBusy at once instead of holding a connection to wait.
func BuildIndexes(ctx context.Context, db *pgxpool.Pool, indexes []Index) error {
	conn, err := db.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	var locked bool
	if err = conn.QueryRow(ctx, "SELECT pg_try_advisory_lock(hashtextextended('concurrent-indexes:'||current_schema(),0))").Scan(&locked); err != nil {
		return err
	}
	if !locked {
		return errIndexesBusy
	}
	defer func() {
		// the session lock outlives this call: released here, or closed with its connection.
		unlock, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		if _, err := conn.Exec(unlock, "SELECT pg_advisory_unlock_all()"); err != nil {
			_ = conn.Conn().Close(unlock)
		}
	}()
	for _, index := range indexes {
		var schema string
		var valid bool
		err = conn.QueryRow(ctx, `SELECT n.nspname,i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
			JOIN pg_namespace n ON n.oid=c.relnamespace WHERE i.indrelid=to_regclass($1) AND c.relname=$2`, index.Table, index.Name).Scan(&schema, &valid)
		switch {
		case errors.Is(err, pgx.ErrNoRows):
		case err != nil:
			return err
		case valid:
			continue
		default:
			if _, err = conn.Exec(ctx, "DROP INDEX CONCURRENTLY "+pgx.Identifier{schema, index.Name}.Sanitize()); err != nil {
				return err
			}
		}
		// without if not exists: a same-named index on another table of the schema fails loudly here.
		if _, err = conn.Exec(ctx, "CREATE INDEX CONCURRENTLY "+index.Name+" ON "+index.Table+" "+index.Definition); err != nil {
			return err
		}
	}
	return nil
}

// IndexBuilder is a worker that builds the indexes beside the service's other work, trying again with a
// growing wait after a failure or while another replica builds them, until they are built or ctx ends. a build
// another session has held for the longest wait is logged, so one stuck elsewhere is seen.
func IndexBuilder(db *pgxpool.Pool, indexes ...Index) func(context.Context) {
	return func(ctx context.Context) {
		wait, started := indexRetry, time.Now()
		for {
			err := BuildIndexes(ctx, db, indexes)
			if err == nil || ctx.Err() != nil {
				return
			}
			switch {
			case !errors.Is(err, errIndexesBusy):
				slog.Error("index_build_failed", "error_category", fmt.Sprintf("%T", err), "retry_seconds", wait.Seconds())
			case wait >= indexRetryMax:
				slog.Warn("index_build_waiting", "waited_seconds", time.Since(started).Seconds())
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
			wait = min(2*wait, indexRetryMax)
		}
	}
}
