package platform

import (
	"context"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func TestLocksWaitOnlyForTheSameScopeAndKey(t *testing.T) {
	db := testSchema(t)
	ctx := context.Background()
	begin := func() pgx.Tx {
		tx, err := db.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = tx.Rollback(ctx) })
		return tx
	}
	// takes reports whether lock is granted within a short wait, then ends its transaction.
	takes := func(lock func(context.Context, pgx.Tx, string, string) error, scope, key string) bool {
		tx := begin()
		defer func() { _ = tx.Rollback(ctx) }()
		short, cancel := context.WithTimeout(ctx, 300*time.Millisecond)
		defer cancel()
		return lock(short, tx, scope, key) == nil
	}
	first := begin()
	if err := LockShared(ctx, first, LockClassroom, "a"); err != nil {
		t.Fatal(err)
	}
	if !takes(LockShared, LockClassroom, "a") {
		t.Fatal("a shared lock waited for another shared holder")
	}
	if takes(Lock, LockClassroom, "a") {
		t.Fatal("the exclusive lock was granted while a shared holder ran")
	}
	if !takes(Lock, LockClassroom, "b") || !takes(Lock, LockPrerequisites, "a") {
		t.Fatal("a lock on another key or scope waited")
	}
	if err := first.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if !takes(Lock, LockClassroom, "a") {
		t.Fatal("the exclusive lock was not granted after the shared holders ended")
	}
}
