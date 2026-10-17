package platform

import (
	"context"
	"slices"
	"testing"
	"testing/fstest"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestMigrationsAreOrderedByVersion(t *testing.T) {
	fsys := fstest.MapFS{
		"migrations/002_chunks.sql": {Data: []byte("two")},
		"migrations/001_init.sql":   {Data: []byte("one")},
		"migrations/README.md":      {Data: []byte("ignored")},
	}
	got, err := Migrations(fsys)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].Version != 1 || got[0].SQL != "one" || got[1].Version != 2 || got[1].Name != "002_chunks.sql" {
		t.Fatalf("unexpected migrations: %+v", got)
	}
}

func TestMigrationsRejectGapsDuplicatesAndBadNames(t *testing.T) {
	for name, fsys := range map[string]fstest.MapFS{
		"gap":       {"migrations/001_init.sql": {}, "migrations/003_late.sql": {}},
		"duplicate": {"migrations/001_init.sql": {}, "migrations/001_again.sql": {}},
		"bad name":  {"migrations/001_init.sql": {}, "migrations/2_chunks.sql": {}},
		"empty":     {"migrations/README.md": {}},
	} {
		if _, err := Migrations(fsys); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

// Two migrations that each fail when run twice: the second run must skip both.
var twoMigrations = fstest.MapFS{
	"migrations/001_init.sql": {Data: []byte("CREATE TABLE things(id int PRIMARY KEY)")},
	"migrations/002_seed.sql": {Data: []byte("INSERT INTO things VALUES (1)")},
	"migrations/notes.txt":    {Data: []byte("ignored")},
}

func appliedVersions(t *testing.T, db *pgxpool.Pool) []int {
	t.Helper()
	rows, err := db.Query(context.Background(), "SELECT version FROM schema_migrations ORDER BY version")
	if err != nil {
		t.Fatal(err)
	}
	versions, err := pgx.CollectRows(rows, pgx.RowTo[int])
	if err != nil {
		t.Fatal(err)
	}
	return versions
}

func TestMigrateTwiceIsANoOp(t *testing.T) {
	db := testSchema(t)
	ordered, err := Migrations(twoMigrations)
	if err != nil {
		t.Fatal(err)
	}
	for run := range 2 {
		if err = applyMigrations(context.Background(), db, ordered, EventMigration); err != nil {
			t.Fatalf("run %d: %v", run+1, err)
		}
	}
	var things int
	if err = db.QueryRow(context.Background(), "SELECT count(*) FROM things").Scan(&things); err != nil || things != 1 {
		t.Fatalf("things %d, %v", things, err)
	}
	if got := appliedVersions(t, db); !slices.Equal(got, []int{1, 2}) {
		t.Fatalf("recorded versions %v", got)
	}
}

func TestConcurrentStartsMigrateOnce(t *testing.T) {
	db := testSchema(t)
	ordered, err := Migrations(twoMigrations)
	if err != nil {
		t.Fatal(err)
	}
	errs := make(chan error, 4)
	for range cap(errs) {
		go func() { errs <- applyMigrations(context.Background(), db, ordered, EventMigration) }()
	}
	for range cap(errs) {
		if err = <-errs; err != nil {
			t.Error(err)
		}
	}
	if got := appliedVersions(t, db); !slices.Equal(got, []int{1, 2}) {
		t.Fatalf("recorded versions %v", got)
	}
}
