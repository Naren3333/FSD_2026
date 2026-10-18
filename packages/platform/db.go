package platform

import (
	"context"
	"io/fs"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ExecAll runs the statements in order inside tx, each with the same arguments, and stops at the first that fails.
// A purge deletes what it holds for a classroom, assessment or user this way, statement by statement.
func ExecAll(ctx context.Context, tx pgx.Tx, statements []string, args ...any) error {
	for _, sql := range statements {
		if _, err := tx.Exec(ctx, sql, args...); err != nil {
			return err
		}
	}
	return nil
}

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
	if err = Lock(ctx, tx, LockMigrations, ""); err != nil {
		return err
	}
	if err = migrate(ctx, tx, ordered, bootstrap); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
