package platformtest

import (
	"context"
	"io/fs"
	"learning/platform"
	"os"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// db is a pool on a throwaway schema of the postgresql database named by the environment variable env, with
// every migration in fsys's migrations directory and the outbox and inbox tables applied.
func DB(t testing.TB, env string, fsys fs.FS) *pgxpool.Pool {
	t.Helper()
	url := os.Getenv(env)
	if url == "" {
		t.Skip(env + " is not set")
	}
	ctx := context.Background()
	schema := "test_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:12]
	admin, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), "DROP SCHEMA "+schema+" CASCADE")
		admin.Close()
	})
	config, err := pgxpool.ParseConfig(url)
	if err != nil {
		t.Fatal(err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema + ",public"
	db, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	if fsys != nil {
		ordered, err := platform.Migrations(fsys)
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range ordered {
			if _, err = db.Exec(ctx, m.SQL); err != nil {
				t.Fatalf("%s: %v", m.Name, err)
			}
		}
	}
	if _, err = db.Exec(ctx, platform.EventMigration); err != nil {
		t.Fatal(err)
	}
	return db
}
