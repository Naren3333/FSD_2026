package platform

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/segmentio/kafka-go"
)

const consumerTopic = "test.happened"

// envelope is a valid event message of consumertopic, changed by edit before it is encoded.
func envelope(t *testing.T, edit func(map[string]any)) kafka.Message {
	t.Helper()
	e := map[string]any{"event_id": uuid.NewString(), "event_type": consumerTopic, "schema_version": 1, "timestamp": time.Now().UTC(), "correlation_id": uuid.NewString(), "resource_id": "r1", "data": map[string]any{}}
	if edit != nil {
		edit(e)
	}
	value, err := json.Marshal(e)
	if err != nil {
		t.Fatal(err)
	}
	return kafka.Message{Topic: consumerTopic, Key: []byte("r1"), Value: value}
}

// dlq is a dead-letter writer that takes everything, or fails everything when broken.
func dlq(broken bool) *fakeWriter {
	return &fakeWriter{fail: func(kafka.Message) bool { return broken }}
}

func inboxed(t *testing.T, db *pgxpool.Pool, msg kafka.Message) bool {
	t.Helper()
	var e Event
	var found bool
	if err := json.Unmarshal(msg.Value, &e); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(context.Background(), "SELECT EXISTS(SELECT 1 FROM inbox WHERE event_id=$1)", e.ID).Scan(&found); err != nil {
		t.Fatal(err)
	}
	return found
}

func TestSettleCommitsAnInvalidEnvelopeOnlyOnceDeadLettered(t *testing.T) {
	never := func(context.Context, pgx.Tx, Event) error { t.Fatal("handler ran for an invalid envelope"); return nil }
	for name, edit := range map[string]func(map[string]any){
		"other topic":    func(e map[string]any) { e["event_type"] = "other.happened" },
		"version 2":      func(e map[string]any) { e["schema_version"] = 2 },
		"bad event id":   func(e map[string]any) { e["event_id"] = "x" },
		"no correlation": func(e map[string]any) { delete(e, "correlation_id") },
		"no timestamp":   func(e map[string]any) { delete(e, "timestamp") },
		"no resource":    func(e map[string]any) { e["resource_id"] = "" },
	} {
		msg := envelope(t, edit)
		for _, broken := range []bool{false, true} {
			w := dlq(broken)
			if committed := (Consumer{}).settle(context.Background(), consumerTopic, msg, never, w); committed == broken {
				t.Errorf("%s: committed %v with dead-letter write failing %v", name, committed, broken)
			}
			if len(w.given) != 1 || w.given[0].Topic != consumerTopic+".dlq" || string(w.given[0].Value) != string(msg.Value) {
				t.Errorf("%s: dead-lettered %v", name, w.given)
			}
		}
	}
	w := dlq(false)
	if !(Consumer{}).settle(context.Background(), consumerTopic, kafka.Message{Value: []byte("not json")}, never, w) || len(w.sent) != 1 {
		t.Error("unreadable message was not dead-lettered and committed")
	}
}

func TestSettleCommitsAnInvalidEventOnlyOnceDeadLettered(t *testing.T) {
	db := eventDB(t)
	invalid := func(context.Context, pgx.Tx, Event) error { return InvalidEvent{"no such classroom"} }
	for _, broken := range []bool{false, true} {
		msg, w := envelope(t, nil), dlq(broken)
		if committed := (Consumer{DB: db}).settle(context.Background(), consumerTopic, msg, invalid, w); committed == broken {
			t.Errorf("committed %v with dead-letter write failing %v", committed, broken)
		}
		if len(w.given) != 1 || w.given[0].Topic != consumerTopic+".dlq" {
			t.Errorf("dead-lettered %v", w.given)
		}
		if inboxed(t, db, msg) {
			t.Error("an invalid event was recorded as processed")
		}
	}
}

func TestSettleLeavesAFailedEventUncommittedForARetry(t *testing.T) {
	db := eventDB(t)
	msg, w := envelope(t, nil), dlq(false)
	ctx, cancel := context.WithCancel(context.Background())
	runs := 0
	failing := func(context.Context, pgx.Tx, Event) error {
		runs++
		cancel() // shutdown during the retry backoff ends the attempts
		return errors.New("database down")
	}
	if (Consumer{DB: db}).settle(ctx, consumerTopic, msg, failing, w) {
		t.Fatal("a failed event was committed")
	}
	if runs != 1 || len(w.given) != 0 || inboxed(t, db, msg) {
		t.Fatalf("runs %d, dead-lettered %d, inbox kept the failed event %v", runs, len(w.given), inboxed(t, db, msg))
	}
	event, _ := decodeEvent(msg.Value, consumerTopic)
	broken := func(context.Context, pgx.Tx, Event) error { runs++; return errors.New("database down") }
	if err := (Consumer{DB: db}).handleOnce(context.Background(), event, broken); err == nil || inboxed(t, db, msg) {
		t.Fatalf("a failed attempt was recorded as processed: %v", err)
	}
	succeeding := func(context.Context, pgx.Tx, Event) error { runs++; return nil }
	if !(Consumer{DB: db}).settle(context.Background(), consumerTopic, msg, succeeding, w) || runs != 3 || !inboxed(t, db, msg) {
		t.Fatalf("the redelivered event was not processed: runs %d", runs)
	}
}

func TestSettleRunsTheHandlerOncePerEvent(t *testing.T) {
	db := eventDB(t)
	msg, w := envelope(t, nil), dlq(false)
	runs := 0
	counting := func(context.Context, pgx.Tx, Event) error { runs++; return nil }
	for range 3 {
		if !(Consumer{DB: db}).settle(context.Background(), consumerTopic, msg, counting, w) {
			t.Fatal("a delivery was not committed")
		}
	}
	if runs != 1 || len(w.given) != 0 {
		t.Fatalf("handler ran %d times for one event", runs)
	}
}

func TestAnAttemptGetsTheConsumersTimeout(t *testing.T) {
	db := eventDB(t)
	for want, c := range map[time.Duration]Consumer{defaultEventTimeout: {DB: db}, 5 * time.Minute: {DB: db, Timeout: 5 * time.Minute}} {
		msg := envelope(t, nil)
		event, _ := decodeEvent(msg.Value, consumerTopic)
		var left time.Duration
		measure := func(ctx context.Context, _ pgx.Tx, _ Event) error {
			deadline, _ := ctx.Deadline()
			left = time.Until(deadline)
			return nil
		}
		if err := c.handleOnce(context.Background(), event, measure); err != nil {
			t.Fatal(err)
		}
		if left <= want-5*time.Second || left > want {
			t.Fatalf("an attempt had %v left, want about %v", left, want)
		}
	}
}
