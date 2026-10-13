package platform

import (
	"context"
	"io/fs"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// migrationLock serializes migrations when several replicas of a service start at once.
const migrationLock = 314159

// OpenDB connects to url and applies the service's numbered migrations (migrations/NNN_name.sql in the given file
// system, normally an embed.FS) plus bootstrap, under an advisory lock, before returning the pool.
func OpenDB(ctx context.Context, url string, migrations fs.FS, bootstrap string) (*pgxpool.Pool, error) {
	ordered, err := Migrations(migrations)
	if err != nil {
		return nil, err
	}
	config, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	config.MaxConns = 10
	config.MinConns = 1
	config.MaxConnLifetime = 30 * time.Minute
	db, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, err
	}
	if err = applyMigrations(ctx, db, ordered, bootstrap); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func applyMigrations(ctx context.Context, db *pgxpool.Pool, ordered []Migration, bootstrap string) error {
	if err := db.Ping(ctx); err != nil {
		return err
	}
	tx, err := db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", migrationLock); err != nil {
		return err
	}
	if err = migrate(ctx, tx, ordered, bootstrap); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
