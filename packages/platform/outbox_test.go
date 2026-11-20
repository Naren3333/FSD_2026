package platform

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

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
	if err = DiscardPendingBy(ctx, tx, "a.submitted.%", PurgeByStudent, "s1", "o"); err != nil {
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

// purges and prunes find their events through the outbox's indexes, and a key that is no purge key is refused.
func TestPurgesAndPrunesUseTheOutboxIndexes(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	indexes := append(OutboxIndexes(), allPurgeIndexes()...)
	// built twice, as by a restarted service: the second build finds the indexes and changes nothing.
	for range 2 {
		if err := BuildIndexes(ctx, db, indexes); err != nil {
			t.Fatal(err)
		}
	}
	var valid int
	if err := db.QueryRow(ctx, "SELECT count(*) FROM pg_index WHERE indexrelid IN (to_regclass('outbox_published'),to_regclass('outbox_data_classroom'),to_regclass('outbox_data_assessment'),to_regclass('outbox_data_student')) AND indisvalid").Scan(&valid); err != nil || valid != 4 {
		t.Fatalf("valid outbox indexes = %d, %v", valid, err)
	}
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if err = DiscardPendingBy(ctx, tx, "%", PurgeKey{}, "a/1", "o"); err == nil {
		t.Fatal("discarded by no purge key")
	}
	if _, err = tx.Exec(ctx, "SET LOCAL enable_seqscan=off"); err != nil {
		t.Fatal(err)
	}
	statements := []struct {
		sql, index string
		args       []any
	}{{pruneSQL, "outbox_published", []any{OutboxRetention.Seconds(), pruneBatch}}}
	for _, key := range purgeKeys {
		statements = append(statements, struct {
			sql, index string
			args       []any
		}{discardSQL(key), key.index, []any{"%", "v", "o"}})
	}
	for _, s := range statements {
		var plan []string
		rows, err := tx.Query(ctx, "EXPLAIN "+s.sql, s.args...)
		if err == nil {
			plan, err = pgx.CollectRows(rows, pgx.RowTo[string])
		}
		if err != nil || !slices.ContainsFunc(plan, func(line string) bool { return strings.Contains(line, s.index) }) {
			t.Fatalf("%s: plan %v, %v", s.index, plan, err)
		}
	}
}

// the purge indexes build while the outbox takes events, and a build interrupted earlier is replaced.
func TestPurgeIndexesBuildBesideInsertsAndReplaceAnInvalidOne(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	insert := `INSERT INTO outbox(id,topic,partition_key,body) VALUES(gen_random_uuid(),'t','k','{"data":{"student_id":"s"}}')`
	held, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Rollback(ctx)
	// an uncommitted insert, as a business transaction under way: the concurrent build waits for it.
	if _, err = held.Exec(ctx, insert); err != nil {
		t.Fatal(err)
	}
	buildCtx, stop := context.WithCancel(ctx)
	built := make(chan error, 1)
	go func() { built <- BuildIndexes(buildCtx, db, allPurgeIndexes()) }()
	validity := func(name string) (exists, valid bool) {
		_ = db.QueryRow(ctx, "SELECT count(*)>0,coalesce(bool_and(indisvalid),false) FROM pg_index WHERE indexrelid=to_regclass($1)", name).Scan(&exists, &valid)
		return exists, valid
	}
	eventually(t, func() bool { exists, _ := validity("outbox_data_assessment"); return exists }, "the build did not start")
	// a build that held inserts, as a plain create index does, would block this one.
	inserted, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if _, err = db.Exec(inserted, insert); err != nil {
		t.Fatalf("an insert waited on the build: %v", err)
	}
	// interrupted, as by a restart, the build leaves its index invalid.
	stop()
	if err = <-built; err == nil {
		t.Fatal("the interrupted build reported success")
	}
	if exists, valid := validity("outbox_data_assessment"); !exists || valid {
		t.Fatalf("interrupted index: exists %v, valid %v", exists, valid)
	}
	if err = held.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	// the interrupted session may hold its lock a moment longer, until its server process has ended.
	eventually(t, func() bool {
		err = BuildIndexes(ctx, db, allPurgeIndexes())
		return !errors.Is(err, errIndexesBusy)
	}, "the interrupted build's lock was never released")
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range purgeKeys {
		if exists, valid := validity(key.index); !exists || !valid {
			t.Fatalf("%s: exists %v, valid %v", key.index, exists, valid)
		}
	}
}

func TestOutboxDrainSendsAKeysEventsInOrderWithoutWaitingForATick(t *testing.T) {
	db := eventDB(t)
	emit(t, db, "a/1", "a/2", "a/3", "b/1", "a/4")
	w := &fakeWriter{}
	(Outbox{DB: db}).drain(context.Background(), w, time.Now().Add(time.Minute))
	got := names(t, w.sent)
	if !slices.Equal(got, []string{"a/1", "b/1", "a/2", "a/3", "a/4"}) {
		t.Fatalf("drained %v", got)
	}
}

func TestOutboxDrainStopsWhenAnEventFails(t *testing.T) {
	db := eventDB(t)
	emit(t, db, "a/1", "a/2", "b/1")
	w := &fakeWriter{fail: func(m kafka.Message) bool { return string(m.Key) == "a" }}
	(Outbox{DB: db}).drain(context.Background(), w, time.Now().Add(time.Minute))
	if got := names(t, w.given); !slices.Equal(got, []string{"a/1", "b/1"}) {
		t.Fatalf("given %v; a failed batch must end the drain until the retry is due", got)
	}
}

// past its deadline a drain stops after the batch under way, leaving the rest for the next tick.
func TestOutboxDrainStopsAtItsDeadline(t *testing.T) {
	db := eventDB(t)
	emit(t, db, "a/1", "a/2", "a/3")
	w := &fakeWriter{}
	(Outbox{DB: db}).drain(context.Background(), w, time.Now())
	if got := names(t, w.sent); !slices.Equal(got, []string{"a/1"}) {
		t.Fatalf("drained %v past its deadline", got)
	}
}

// eventually fails the test unless done holds within five seconds.
func eventually(t *testing.T, done func() bool, failure string) {
	t.Helper()
	for deadline := time.Now().Add(5 * time.Second); !done(); time.Sleep(50 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal(failure)
		}
	}
}

// events go out while a prune waits on a lock, and the relay stops only once its housekeeping has.
func TestOutboxPublishesWhileItPrunes(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	if _, err := db.Exec(ctx, `INSERT INTO outbox(id,topic,partition_key,body,published_at) VALUES(gen_random_uuid(),'t','k','{}',now()-interval '8 days')`); err != nil {
		t.Fatal(err)
	}
	held, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Rollback(ctx)
	if _, err = held.Exec(ctx, "SELECT 1 FROM outbox FOR UPDATE"); err != nil {
		t.Fatal(err)
	}
	emit(t, db, "a/1")
	w := &fakeWriter{}
	runCtx, stop := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		(Outbox{DB: db}).run(runCtx, w)
		close(done)
	}()
	pruneWaiting := func() bool {
		var waiting bool
		err := db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
			AND wait_event_type='Lock' AND query LIKE 'DELETE FROM outbox WHERE id IN%')`).Scan(&waiting)
		return err == nil && waiting
	}
	sent := func() bool {
		w.mu.Lock()
		defer w.mu.Unlock()
		return len(w.sent) == 1
	}
	eventually(t, pruneWaiting, "no prune waiting on the held row")
	eventually(t, sent, "nothing published while the prune waited")
	if !pruneWaiting() {
		t.Fatal("the prune stopped waiting before the event was published")
	}
	stop()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the relay did not stop")
	}
}

func TestOutboxPrunesEveryBatchOfOldEvents(t *testing.T) {
	db := eventDB(t)
	ctx := context.Background()
	if _, err := db.Exec(ctx, `INSERT INTO outbox(id,topic,partition_key,body,published_at)
		SELECT gen_random_uuid(),'t','k','{}',now()-interval '8 days' FROM generate_series(1,$1::int)`, 2*pruneBatch+5); err != nil {
		t.Fatal(err)
	}
	pruned, err := pruneAll(ctx, db, OutboxRetention)
	var left int
	if err == nil {
		err = db.QueryRow(ctx, "SELECT count(*) FROM outbox").Scan(&left)
	}
	if err != nil || pruned != 2*pruneBatch+5 || left != 0 {
		t.Fatalf("pruned %d, left %d, %v", pruned, left, err)
	}
}
