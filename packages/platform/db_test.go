package platform

import (
	"context"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/segmentio/kafka-go"
)

// testschema is a pool on an empty throwaway schema of platform_test_database_url, dropped when the test
// ends; without the variable the test is skipped.
func testSchema(t *testing.T) *pgxpool.Pool {
	t.Helper()
	url := os.Getenv("PLATFORM_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("PLATFORM_TEST_DATABASE_URL is not set")
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
	config.ConnConfig.RuntimeParams["search_path"] = schema
	db, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return db
}

// eventdb is testschema with the outbox and inbox tables.
func eventDB(t *testing.T) *pgxpool.Pool {
	t.Helper()
	db := testSchema(t)
	if _, err := db.Exec(context.Background(), EventMigration); err != nil {
		t.Fatal(err)
	}
	return db
}

// fakewriter stands in for kafka: it fails the messages fail picks, answering with kafka.writeerrors as the
// real writer does, and records every message it was given and every one it took.
type fakeWriter struct {
	mu          sync.Mutex
	fail        func(kafka.Message) bool
	given, sent []kafka.Message
}

func (f *fakeWriter) WriteMessages(_ context.Context, msgs ...kafka.Message) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.given = append(f.given, msgs...)
	errs := make(kafka.WriteErrors, len(msgs))
	failed := false
	for i, m := range msgs {
		if f.fail != nil && f.fail(m) {
			errs[i] = kafka.LeaderNotAvailable
			failed = true
		} else {
			f.sent = append(f.sent, m)
		}
	}
	if failed {
		return errs
	}
	return nil
}

// reset forgets what the writer was given and took.
func (f *fakeWriter) reset() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.given, f.sent = nil, nil
}
