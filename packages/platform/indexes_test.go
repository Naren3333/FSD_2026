package platform

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// purgeKeys are every purge key, in the order their indexes are built in tests.
var purgeKeys = []PurgeKey{PurgeByAssessment, PurgeByClassroom, PurgeByStudent}

// allPurgeIndexes are the outbox indexes of every purge key.
func allPurgeIndexes() []Index { return PurgeIndexes(purgeKeys...) }

// a service builds only the indexes of the keys it names, and the zero key panics.
func TestPurgeIndexesAreTheNamedKeys(t *testing.T) {
	if got := PurgeIndexes(PurgeByClassroom); len(got) != 1 || got[0].Name != "outbox_data_classroom" || got[0].Table != "outbox" {
		t.Fatalf("indexes %+v", got)
	}
	defer func() {
		if recover() == nil {
			t.Fatal("the zero key did not panic")
		}
	}()
	PurgeIndexes(PurgeKey{})
}

// an invalid index on the target table is rebuilt although a valid one of the same name, on another table in
// another schema of the search path, is found first by name, and that one is left alone.
func TestAnInvalidIndexIsFoundOnItsOwnTable(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	var schema string
	if err := db.QueryRow(ctx, "SELECT current_schema()").Scan(&schema); err != nil {
		t.Fatal(err)
	}
	decoy := schema + "_decoy"
	if _, err := db.Exec(ctx, "CREATE SCHEMA "+decoy+"; CREATE TABLE "+decoy+".other(x text); CREATE INDEX outbox_data_student ON "+decoy+".other(x)"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = db.Exec(context.Background(), "DROP SCHEMA "+decoy+" CASCADE") })
	// interrupted while it waits on an uncommitted insert, the target's build is left invalid.
	held, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Rollback(ctx)
	if _, err = held.Exec(ctx, `INSERT INTO outbox(id,topic,partition_key,body) VALUES(gen_random_uuid(),'t','k','{}')`); err != nil {
		t.Fatal(err)
	}
	state := func() (exists, valid bool) {
		_ = db.QueryRow(ctx, `SELECT count(*)>0,coalesce(bool_and(i.indisvalid),false) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
			WHERE i.indrelid='outbox'::regclass AND c.relname='outbox_data_student'`).Scan(&exists, &valid)
		return exists, valid
	}
	buildCtx, stop := context.WithCancel(ctx)
	built := make(chan error, 1)
	go func() { built <- BuildIndexes(buildCtx, db, PurgeIndexes(PurgeByStudent)) }()
	eventually(t, func() bool { exists, _ := state(); return exists }, "the build did not start")
	stop()
	<-built
	if exists, valid := state(); !exists || valid {
		t.Fatalf("interrupted: exists %v, valid %v", exists, valid)
	}
	if err = held.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	// the decoy's schema first on the path, as a bare name would resolve to its index.
	config, err := pgxpool.ParseConfig(os.Getenv("PLATFORM_TEST_DATABASE_URL"))
	if err != nil {
		t.Fatal(err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = decoy + "," + schema
	both, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	defer both.Close()
	eventually(t, func() bool {
		err = BuildIndexes(ctx, both, PurgeIndexes(PurgeByStudent))
		return !errors.Is(err, errIndexesBusy)
	}, "the interrupted build's lock was never released")
	if err != nil {
		t.Fatal(err)
	}
	if exists, valid := state(); !exists || !valid {
		t.Fatalf("rebuilt: exists %v, valid %v", exists, valid)
	}
	var kept bool
	if err = db.QueryRow(ctx, "SELECT to_regclass($1) IS NOT NULL", decoy+".outbox_data_student").Scan(&kept); err != nil || !kept {
		t.Fatalf("the decoy's index was dropped: %v", err)
	}
}

// while another session builds, a build returns at once instead of holding a connection to wait, and the
// builder tries again until the indexes are built.
func TestAnIndexBuildInProgressElsewhereIsRetriedLater(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	other, err := db.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Release()
	if _, err = other.Exec(ctx, "SELECT pg_advisory_lock(hashtextextended('concurrent-indexes:'||current_schema(),0))"); err != nil {
		t.Fatal(err)
	}
	if err = BuildIndexes(ctx, db, allPurgeIndexes()); !errors.Is(err, errIndexesBusy) {
		t.Fatalf("a build beside another: %v", err)
	}
	retry, retryMax := indexRetry, indexRetryMax
	indexRetry, indexRetryMax = 20*time.Millisecond, 50*time.Millisecond
	defer func() { indexRetry, indexRetryMax = retry, retryMax }()
	runCtx, stop := context.WithCancel(ctx)
	defer stop()
	done := make(chan struct{})
	go func() {
		IndexBuilder(db, allPurgeIndexes()...)(runCtx)
		close(done)
	}()
	time.Sleep(100 * time.Millisecond)
	if _, err = other.Exec(ctx, "SELECT pg_advisory_unlock_all()"); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("the builder did not finish once the other build was done")
	}
	var valid int
	if err = db.QueryRow(ctx, "SELECT count(*) FROM pg_index WHERE indexrelid IN (to_regclass('outbox_data_classroom'),to_regclass('outbox_data_assessment'),to_regclass('outbox_data_student')) AND indisvalid").Scan(&valid); err != nil || valid != 3 {
		t.Fatalf("valid indexes %d, %v", valid, err)
	}
	// and the lock was released, so the next build runs.
	if err = BuildIndexes(ctx, db, allPurgeIndexes()); err != nil {
		t.Fatal(err)
	}
}

// a builder stops when its service does, even while it waits to try again.
func TestAnIndexBuilderStopsWithItsService(t *testing.T) {
	db := eventDB(t)
	runCtx, stop := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		IndexBuilder(db, Index{Name: "broken", Table: "missing_table", Definition: "(x)"})(runCtx)
		close(done)
	}()
	time.Sleep(100 * time.Millisecond)
	stop()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the builder did not stop")
	}
}

// starting again on an existing outbox takes no lock that waits on a concurrent build there, and an outbox
// made before created_at defaulted to clock_timestamp() is changed.
func TestEventMigrationLocksAnExistingOutboxOnlyToChangeIt(t *testing.T) {
	db := testSchema(t)
	ctx := context.Background()
	if _, err := db.Exec(ctx, "CREATE TABLE outbox (id uuid PRIMARY KEY, topic text NOT NULL, partition_key text NOT NULL, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz, attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now())"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(ctx, EventMigration); err != nil {
		t.Fatal(err)
	}
	var fallback string
	if err := db.QueryRow(ctx, "SELECT column_default FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='outbox' AND column_name='created_at'").Scan(&fallback); err != nil || fallback != "clock_timestamp()" {
		t.Fatalf("created_at defaults to %q, %v", fallback, err)
	}
	build, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer build.Rollback(ctx)
	// the lock a concurrent index build holds on the table while it runs.
	if _, err = build.Exec(ctx, "LOCK TABLE outbox IN SHARE UPDATE EXCLUSIVE MODE"); err != nil {
		t.Fatal(err)
	}
	start, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer start.Rollback(ctx)
	if _, err = start.Exec(ctx, "SET LOCAL lock_timeout='2s'"); err != nil {
		t.Fatal(err)
	}
	if _, err = start.Exec(ctx, EventMigration); err != nil {
		t.Fatalf("a start waited on the build: %v", err)
	}
}
