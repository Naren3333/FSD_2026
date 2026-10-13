package platform

import (
	"testing"
	"testing/fstest"
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
