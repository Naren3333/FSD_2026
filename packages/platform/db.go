package platform

import (
	"context"
	"fmt"
	"io/fs"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// execall runs the statements in order inside tx, each with the same arguments, and stops at the first that
// fails.
func ExecAll(ctx context.Context, tx pgx.Tx, statements []string, args ...any) error {
	for _, sql := range statements {
		if _, err := tx.Exec(ctx, sql, args...); err != nil {
			return err
		}
	}
	return nil
}

// connect and apply service migrations under an advisory lock.
func OpenDB(ctx context.Context, url string, migrations fs.FS, bootstrap string) (*pgxpool.Pool, error) {
	ordered, err := Migrations(migrations)
	if err != nil {
		return nil, err
	}
	config, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	maximum, err := strconv.Atoi(Env("DB_POOL_MAX", "10"))
	if err != nil || maximum < 1 || maximum > 1000 {
		return nil, fmt.Errorf("DB_POOL_MAX must be 1–1000")
	}
	minimum, err := strconv.Atoi(Env("DB_POOL_MIN", "1"))
	if err != nil || minimum < 0 || minimum > maximum {
		return nil, fmt.Errorf("DB_POOL_MIN must be 0–DB_POOL_MAX")
	}
	config.MaxConns = int32(maximum)
	config.MinConns = int32(minimum)
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
