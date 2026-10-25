package platform

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/segmentio/kafka-go"
)

const outboxTopic = "test.happened"

// emit writes events named "k/n", keyed k, in one transaction in the order given.
func emit(t *testing.T, db *pgxpool.Pool, names ...string) {
	t.Helper()
	ctx := context.Background()
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	for _, name := range names {
		if err = Emit(ctx, tx, outboxTopic, name[:1], map[string]string{"name": name}); err != nil {
			t.Fatal(err)
		}
	}
	if err = tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
}

// names are the events' names, in the order given.
func names(t *testing.T, msgs []kafka.Message) []string {
	t.Helper()
	out := []string{}
	for _, m := range msgs {
		var e Event
		var data struct{ Name string }
		if json.Unmarshal(m.Value, &e) != nil || json.Unmarshal(e.Data, &data) != nil || string(m.Key)+"/"+data.Name[2:] != data.Name {
			t.Fatalf("unexpected message %s", m.Value)
		}
		out = append(out, data.Name)
	}
	return out
}

func publish(t *testing.T, db *pgxpool.Pool, w *fakeWriter) []string {
	t.Helper()
	w.reset()
	if _, err := (Outbox{DB: db}).publishBatch(context.Background(), w); err != nil {
		t.Fatal(err)
	}
	return names(t, w.sent)
}

// makeDue lets backed-off events be retried now.
func makeDue(t *testing.T, db *pgxpool.Pool) {
	t.Helper()
	if _, err := db.Exec(context.Background(), "UPDATE outbox SET next_attempt=now()"); err != nil {
		t.Fatal(err)
	}
}

func TestOutboxPartialFailureHoldsBackTheKeysLaterEvents(t *testing.T) {
	db := eventDB(t)
	emit(t, db, "a/1")
	emit(t, db, "a/2")
	emit(t, db, "b/1")
	w := &fakeWriter{fail: func(m kafka.Message) bool { return string(m.Key) == "a" }}
	if got := publish(t, db, w); !slices.Equal(got, []string{"b/1"}) || !slices.Equal(names(t, w.given), []string{"a/1", "b/1"}) {
		t.Fatalf("sent %v of %v; a/2 must wait for a/1", got, names(t, w.given))
	}
	w.fail = nil
	makeDue(t, db)
	if got := publish(t, db, w); !slices.Equal(got, []string{"a/1"}) {
		t.Fatalf("retry sent %v", got)
	}
	if got := publish(t, db, w); !slices.Equal(got, []string{"a/2"}) {
		t.Fatalf("after a/1 sent %v", got)
	}
}

func TestOutboxSkipsAKeyWhoseOldestEventAnotherRelayHolds(t *testing.T) {
	db := eventDB(t)
	emit(t, db, "a/1", "a/2", "b/1")
	ctx := context.Background()
	other, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Rollback(ctx)
	var held []string
	rows, err := other.Query(ctx, "SELECT body->'data'->>'name' FROM outbox WHERE partition_key='a' ORDER BY created_at LIMIT 1 FOR UPDATE")
	if err == nil {
		held, err = pgx.CollectRows(rows, pgx.RowTo[string])
	}
	if err != nil || !slices.Equal(held, []string{"a/1"}) {
		t.Fatalf("lock: %v %v", held, err)
	}
	w := &fakeWriter{}
	if got := publish(t, db, w); !slices.Equal(got, []string{"b/1"}) {
		t.Fatalf("sent %v while a/1 was held", got)
	}
}

func TestOutboxBacksOffThenAbandonsAFailingEvent(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	emit(t, db, "a/1", "a/2")
	w := &fakeWriter{fail: func(m kafka.Message) bool { return true }}
	if got := publish(t, db, w); len(got) != 0 {
		t.Fatalf("sent %v", got)
	}
	var attempts int
	var later bool
	if err := db.QueryRow(ctx, "SELECT attempts,next_attempt>now() FROM outbox WHERE body->'data'->>'name'='a/1'").Scan(&attempts, &later); err != nil || attempts != 1 || !later {
		t.Fatalf("attempts %d, backed off %v, %v", attempts, later, err)
	}
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	due, err := dueRows(ctx, tx)
	_ = tx.Rollback(ctx)
	if err != nil || len(due) != 0 {
		t.Fatalf("due while backing off: %v %v", due, err)
	}
	if _, err = db.Exec(ctx, "UPDATE outbox SET attempts=$1-1,next_attempt=now() WHERE body->'data'->>'name'='a/1'", OutboxMaxAttempts); err != nil {
		t.Fatal(err)
	}
	if got := publish(t, db, w); len(got) != 0 || !slices.Equal(names(t, w.given), []string{"a/1"}) {
		t.Fatalf("last attempt sent %v of %v", got, names(t, w.given))
	}
	w.fail = nil
	makeDue(t, db)
	if got := publish(t, db, w); !slices.Equal(got, []string{"a/2"}) {
		t.Fatalf("an abandoned event must not block its key: sent %v", got)
	}
	if got := publish(t, db, w); len(got) != 0 {
		t.Fatalf("the abandoned event was sent again: %v", got)
	}
}

func TestOutboxKeysAreIndependent(t *testing.T) {
	db := eventDB(t)
	emit(t, db, "a/1", "a/2", "b/1", "c/1", "b/2")
	w := &fakeWriter{}
	if got := publish(t, db, w); !slices.Equal(got, []string{"a/1", "b/1", "c/1"}) {
		t.Fatalf("first batch %v", got)
	}
	if got := publish(t, db, w); !slices.Equal(got, []string{"a/2", "b/2"}) {
		t.Fatalf("second batch %v", got)
	}
}

func TestOutboxPublishesEventsOfOneTransactionInEmitOrder(t *testing.T) {
	db := eventDB(t)
	want := []string{}
	for i := range 20 {
		want = append(want, fmt.Sprintf("a/%d", i))
	}
	emit(t, db, want...)
	w := &fakeWriter{}
	got := []string{}
	for range want {
		got = append(got, publish(t, db, w)...)
	}
	if !slices.Equal(got, want) {
		t.Fatalf("published %v", got)
	}
}

func TestOutboxBatchErrorFailsEveryMessage(t *testing.T) {
	db := eventDB(t)
	emit(t, db, "a/1", "b/1")
	broken := writerFunc(func(context.Context, ...kafka.Message) error { return errors.New("broker down") })
	sent, err := (Outbox{DB: db}).publishBatch(context.Background(), broken)
	var failed int
	if err == nil {
		err = db.QueryRow(context.Background(), "SELECT count(*) FROM outbox WHERE attempts=1 AND published_at IS NULL").Scan(&failed)
	}
	if err != nil || sent != 0 || failed != 2 {
		t.Fatalf("sent %d, failed %d, %v", sent, failed, err)
	}
}

type writerFunc func(context.Context, ...kafka.Message) error

func (f writerFunc) WriteMessages(ctx context.Context, msgs ...kafka.Message) error {
	return f(ctx, msgs...)
}

func TestOutboxPrunesOnlyEventsPublishedLongAgo(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	emit(t, db, "a/1", "b/1", "c/1")
	// a/1 was published long ago, b/1 just now, and c/1 never was, however old.
	for _, sql := range []string{
		"UPDATE outbox SET published_at=now()-interval '8 days' WHERE body->'data'->>'name'='a/1'",
		"UPDATE outbox SET published_at=now() WHERE body->'data'->>'name'='b/1'",
		"UPDATE outbox SET created_at=now()-interval '30 days' WHERE body->'data'->>'name'='c/1'",
	} {
		if _, err := db.Exec(ctx, sql); err != nil {
			t.Fatal(err)
		}
	}
	pruned, err := pruneOutbox(ctx, db, OutboxRetention)
	if err != nil || pruned != 1 {
		t.Fatalf("pruned %d, %v", pruned, err)
	}
	var left []string
	if err = db.QueryRow(ctx, "SELECT array_agg(body->'data'->>'name' ORDER BY body->'data'->>'name') FROM outbox").Scan(&left); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(left, []string{"b/1", "c/1"}) {
		t.Fatalf("left %v", left)
	}
}

func TestDiscardPendingByRemovesOnlyTheMatchingEvents(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	for _, e := range []struct{ topic, name, student, org string }{
		{"a.submitted.v1", "gone/old", "s1", "o"},
		{"a.submitted.v2", "gone/new", "s1", "o"},
		{"a.other.v1", "kept/topic", "s1", "o"},
		{"a.submitted.v2", "kept/student", "s2", "o"},
		{"a.submitted.v2", "kept/org", "s1", "p"},
	} {
		if err = Emit(ctx, tx, e.topic, "k", map[string]string{"name": e.name, "student_id": e.student, "org_id": e.org}); err != nil {
			t.Fatal(err)
		}
	}
	if err = ExecAll(ctx, tx, []string{"SELECT $1::text", "SELECT length($1::text)"}, "x"); err != nil {
		t.Fatal(err)
	}
	if err = DiscardPendingBy(ctx, tx, "a.submitted.%", "student_id", "s1", "o"); err != nil {
		t.Fatal(err)
	}
	var left []string
	if err = tx.QueryRow(ctx, "SELECT array_agg(body->'data'->>'name' ORDER BY body->'data'->>'name') FROM outbox").Scan(&left); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(left, []string{"kept/org", "kept/student", "kept/topic"}) {
		t.Fatalf("left %v", left)
	}
	if err = ExecAll(ctx, tx, []string{"SELECT 1", "SELECT * FROM missing_table", "DELETE FROM outbox"}); err == nil {
		t.Fatal("a failing statement was not reported")
	}
}
