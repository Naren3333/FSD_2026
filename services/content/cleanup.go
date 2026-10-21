package main

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// cleanup is recorded in the deletion transaction and runs only after commit.
type cleaner struct {
	db    *pgxpool.Pool
	files folders
}

func (c cleaner) run(ctx context.Context) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		for ctx.Err() == nil {
			found, err := c.next(ctx)
			if err != nil {
				slog.Error("object_cleanup_failed", "error_category", "storage_or_database")
			}
			if !found || err != nil {
				break
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (c cleaner) next(ctx context.Context) (bool, error) {
	lease := uuid.NewString()
	var prefix string
	var attempts int
	err := c.db.QueryRow(ctx, `WITH due AS (
SELECT prefix FROM object_cleanup_jobs WHERE next_attempt<=now() ORDER BY next_attempt LIMIT 1 FOR UPDATE SKIP LOCKED)
UPDATE object_cleanup_jobs j SET attempts=j.attempts+1,lease=$1,next_attempt=now()+interval '6 minutes'
FROM due WHERE j.prefix=due.prefix RETURNING j.prefix,j.attempts`, lease).Scan(&prefix, &attempts)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	work, cancel := context.WithTimeout(ctx, 5*time.Minute)
	err = c.files.removeAll(work, prefix)
	cancel()
	book, stop := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer stop()
	if err != nil {
		_, updateErr := c.db.Exec(book, "UPDATE object_cleanup_jobs SET lease=NULL,next_attempt=now()+make_interval(secs=>$3) WHERE prefix=$1 AND lease=$2", prefix, lease, backoff(attempts).Seconds())
		if updateErr != nil {
			return true, updateErr
		}
		return true, err
	}
	_, err = c.db.Exec(book, "DELETE FROM object_cleanup_jobs WHERE prefix=$1 AND lease=$2", prefix, lease)
	return true, err
}
