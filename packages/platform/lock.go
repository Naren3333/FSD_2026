package platform

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// Lock scopes. A scope names what a lock protects; with its key (a classroom, an organization, ...) it is hashed
// into one transaction-scoped advisory lock, so locks of different scopes or keys never wait for each other.
// learning_transport.db derives the same key from the same scope and key.
const (
	LockMigrations    = "migrations"    // no key: serializes replicas migrating at once
	LockClassroom     = "classroom"     // a classroom's purge against new rows in it
	LockPrerequisites = "prerequisites" // an organization's prerequisite inserts, against cycles
	LockStudentWork   = "student-work"  // one student's saving and submitting of one assessment
	LockAssessment    = "assessment"    // a deleted assessment's purge against new rows for it
	LockUser          = "user"          // a deleted account's purge against new rows for it
)

const (
	lockSQL       = "SELECT pg_advisory_xact_lock(hashtextextended($1::text || ':' || $2::text, 0))"
	sharedLockSQL = "SELECT pg_advisory_xact_lock_shared(hashtextextended($1::text || ':' || $2::text, 0))"
)

// Lock takes the exclusive advisory lock on (scope, key) until tx ends.
func Lock(ctx context.Context, tx pgx.Tx, scope, key string) error {
	_, err := tx.Exec(ctx, lockSQL, scope, key)
	return err
}

// LockShared takes the advisory lock on (scope, key) shared until tx ends: shared holders run together, and Lock
// waits for all of them.
func LockShared(ctx context.Context, tx pgx.Tx, scope, key string) error {
	_, err := tx.Exec(ctx, sharedLockSQL, scope, key)
	return err
}
