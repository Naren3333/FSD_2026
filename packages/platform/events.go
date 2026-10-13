package platform

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/segmentio/kafka-go"
)

// EventMigration creates the outbox and inbox tables; services that publish or consume events pass it to New.
const EventMigration = `
CREATE TABLE IF NOT EXISTS outbox (id uuid PRIMARY KEY, topic text NOT NULL, partition_key text NOT NULL, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz, attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS inbox (event_id uuid PRIMARY KEY, processed_at timestamptz NOT NULL DEFAULT now());`

// Event is the envelope of every business event on Kafka.
type Event struct {
	ID          string          `json:"event_id"`
	Type        string          `json:"event_type"`
	Version     int             `json:"schema_version"`
	Timestamp   time.Time       `json:"timestamp"`
	Correlation string          `json:"correlation_id"`
	Resource    string          `json:"resource_id"`
	Data        json.RawMessage `json:"data"`
}

// Emit writes an event to the outbox inside the caller's transaction, so it is published only if the change commits.
func Emit(ctx context.Context, tx pgx.Tx, topic, key string, data any) error {
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	e := Event{uuid.NewString(), topic, 1, time.Now().UTC(), Correlation(ctx), key, payload}
	body, err := json.Marshal(e)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, "INSERT INTO outbox(id,topic,partition_key,body) VALUES($1,$2,$3,$4)", e.ID, topic, key, body)
	return err
}

// Writer sends synchronously with every replica's acknowledgement. BatchTimeout matters: kafka-go otherwise holds
// a partial batch for a second before sending it, so every write took about a second.
func Writer() *kafka.Writer {
	return &kafka.Writer{Addr: kafka.TCP(strings.Split(Required("KAFKA_BROKERS"), ",")...), Balancer: &kafka.Hash{}, RequiredAcks: kafka.RequireAll, MaxAttempts: 3, BatchTimeout: 10 * time.Millisecond, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second}
}

// KafkaReady is a readiness check: the first broker answers a metadata request.
func KafkaReady(ctx context.Context) error {
	dialer := &kafka.Dialer{Timeout: 2 * time.Second}
	conn, err := dialer.DialContext(ctx, "tcp", strings.Split(Required("KAFKA_BROKERS"), ",")[0])
	if err != nil {
		return err
	}
	defer conn.Close()
	if err = conn.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		return err
	}
	_, err = conn.Brokers()
	return err
}

// Outbox delivery limits. A row is retried with backoff (doubling, at most five minutes apart) for about a day
// before it needs an operator; every minute any backlog is logged as an error until it clears.
const (
	OutboxBatch       = 100
	OutboxMaxAttempts = 300
)

type outboxRow struct {
	id, topic, key string
	body           []byte
}

// Outbox publishes committed events. Every second it drains due rows in batches of OutboxBatch until none are
// left, so a burst of submissions is delivered in seconds instead of one event per second.
func (a *App) Outbox(ctx context.Context) {
	writer := Writer()
	defer writer.Close()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	var reported time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		for ctx.Err() == nil {
			sent, err := a.publishBatch(ctx, writer)
			if err != nil {
				slog.Error("outbox_batch_failed", "error_category", fmt.Sprintf("%T", err))
				break
			}
			if sent < OutboxBatch {
				break
			}
		}
		if time.Since(reported) >= time.Minute {
			reported = time.Now()
			a.reportOutbox(ctx)
		}
	}
}

// publishBatch sends up to OutboxBatch due rows and records, per row, whether Kafka took it. It returns how many
// rows were published; a failed delivery returns 0 so the rest waits for the next tick instead of hammering the
// broker.
func (a *App) publishBatch(ctx context.Context, writer *kafka.Writer) (int, error) {
	batchCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	tx, err := a.DB.Begin(batchCtx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(batchCtx)
	batch, err := dueRows(batchCtx, tx)
	if err != nil || len(batch) == 0 {
		return 0, err
	}
	messages := make([]kafka.Message, len(batch))
	for i, row := range batch {
		messages[i] = kafka.Message{Topic: row.topic, Key: []byte(row.key), Value: row.body}
	}
	failed := deliveryFailures(writer.WriteMessages(batchCtx, messages...), len(batch))
	published, retry := []string{}, []string{}
	for i, row := range batch {
		if failed[i] {
			retry = append(retry, row.id)
		} else {
			published = append(published, row.id)
		}
	}
	if err = markPublished(batchCtx, tx, published); err != nil {
		return 0, err
	}
	if err = scheduleRetries(batchCtx, tx, retry); err != nil {
		return 0, err
	}
	if err = tx.Commit(batchCtx); err != nil {
		return 0, err
	}
	if len(retry) > 0 {
		return 0, nil
	}
	return len(batch), nil
}

// dueRows locks up to OutboxBatch unpublished rows whose next attempt is due, oldest first.
func dueRows(ctx context.Context, tx pgx.Tx) ([]outboxRow, error) {
	rows, err := tx.Query(ctx, `SELECT id,topic,partition_key,body FROM outbox WHERE published_at IS NULL AND attempts<$1 AND next_attempt<=now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT $2`, OutboxMaxAttempts, OutboxBatch)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	batch := []outboxRow{}
	for rows.Next() {
		var row outboxRow
		if err = rows.Scan(&row.id, &row.topic, &row.key, &row.body); err != nil {
			return nil, err
		}
		batch = append(batch, row)
	}
	return batch, rows.Err()
}

func markPublished(ctx context.Context, tx pgx.Tx, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	_, err := tx.Exec(ctx, "UPDATE outbox SET published_at=now() WHERE id=ANY($1::uuid[])", ids)
	return err
}

// scheduleRetries counts a failed attempt for each row and backs it off, logging the rows that ran out of attempts.
func scheduleRetries(ctx context.Context, tx pgx.Tx, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	slog.Error("outbox_publish_failed", "events", len(ids))
	var abandoned []string
	err := tx.QueryRow(ctx, `WITH failed AS (UPDATE outbox SET attempts=attempts+1,next_attempt=now()+make_interval(secs=>LEAST(300,POWER(2,LEAST(attempts,9))::int)) WHERE id=ANY($1::uuid[]) RETURNING id,attempts)
		SELECT coalesce(array_agg(id::text),'{}') FROM failed WHERE attempts>=$2`, ids, OutboxMaxAttempts).Scan(&abandoned)
	if err != nil {
		return err
	}
	for _, id := range abandoned {
		slog.Error("outbox_event_abandoned", "event_id", id, "attempts", OutboxMaxAttempts)
	}
	return nil
}

// deliveryFailures maps WriteMessages' error to the messages it concerns: kafka.WriteErrors names each message,
// any other error fails them all.
func deliveryFailures(err error, n int) []bool {
	failed := make([]bool, n)
	if err == nil {
		return failed
	}
	var each kafka.WriteErrors
	if errors.As(err, &each) && len(each) == n {
		for i, e := range each {
			failed[i] = e != nil
		}
		return failed
	}
	for i := range failed {
		failed[i] = true
	}
	return failed
}

// reportOutbox logs, as an error, events waiting more than five minutes and events that ran out of attempts, so a
// broker outage or a stuck event is visible instead of silent.
func (a *App) reportOutbox(ctx context.Context) {
	reportCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	var waiting, abandoned int
	err := a.DB.QueryRow(reportCtx, `SELECT count(*) FILTER (WHERE attempts<$1 AND created_at<now()-interval '5 minutes'), count(*) FILTER (WHERE attempts>=$1) FROM outbox WHERE published_at IS NULL`, OutboxMaxAttempts).Scan(&waiting, &abandoned)
	if err != nil {
		slog.Error("outbox_report_failed")
		return
	}
	if waiting > 0 || abandoned > 0 {
		slog.Error("outbox_backlog", "waiting_over_5_minutes", waiting, "abandoned", abandoned)
	}
}

// InvalidEvent is a handler's verdict that an event can never be processed: it is dead-lettered, not retried.
type InvalidEvent struct{ Reason string }

func (e InvalidEvent) Error() string { return e.Reason }

// Handler applies one event inside the transaction that also records it in the inbox.
type Handler func(ctx context.Context, tx pgx.Tx, event Event) error

// Consume processes a topic for as long as ctx lives. A session stops without committing when an event keeps
// failing (the database is down, say); Consume then waits, backing off to a minute, and starts a new session from
// the last committed offset, so the event is retried rather than skipped and the consumer never stays down.
// healthy, if given, is true while a session runs and false while it waits to restart.
func (a *App) Consume(ctx context.Context, topic string, handler Handler, healthy *atomic.Bool) {
	backoff := 5 * time.Second
	for ctx.Err() == nil {
		if healthy != nil {
			healthy.Store(true)
		}
		progressed := a.consumeSession(ctx, topic, handler)
		if healthy != nil {
			healthy.Store(false)
		}
		if ctx.Err() != nil {
			return
		}
		if progressed {
			backoff = 5 * time.Second
		}
		slog.Error("consumer_restarting", "topic", topic, "after_seconds", int(backoff.Seconds()))
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		backoff = min(2*backoff, time.Minute)
	}
}

// consumeSession reads until ctx ends or an event cannot be completed, and reports whether it committed anything.
func (a *App) consumeSession(ctx context.Context, topic string, handler Handler) bool {
	reader := kafka.NewReader(kafka.ReaderConfig{Brokers: strings.Split(Required("KAFKA_BROKERS"), ","), Topic: topic, GroupID: a.Name + "-v1", MinBytes: 1, MaxBytes: 2 << 20, CommitInterval: 0})
	defer reader.Close()
	dlq := Writer()
	defer dlq.Close()
	progressed := false
	for ctx.Err() == nil {
		msg, err := reader.FetchMessage(ctx)
		if err != nil {
			if ctx.Err() == nil {
				slog.Error("consumer_fetch_failed", "topic", topic)
			}
			return progressed
		}
		if !a.settle(ctx, topic, msg, handler, dlq) {
			if ctx.Err() == nil {
				slog.Error("consumer_stopped_uncommitted", "topic", topic)
			}
			return progressed
		}
		if err = reader.CommitMessages(ctx, msg); err != nil {
			slog.Error("consumer_commit_failed", "topic", topic)
			return progressed
		}
		progressed = true
	}
	return progressed
}

// settle handles one message, or dead-letters it when it is invalid, and reports whether it may be committed.
func (a *App) settle(ctx context.Context, topic string, msg kafka.Message, handler Handler, dlq *kafka.Writer) bool {
	event, valid := decodeEvent(msg.Value, topic)
	if valid {
		completed, invalid := a.handle(ctx, event, handler)
		if completed || !invalid {
			return completed
		}
	}
	err := dlq.WriteMessages(ctx, kafka.Message{Topic: topic + ".dlq", Key: msg.Key, Value: msg.Value})
	slog.Error("invalid_event", "event_id", event.ID, "dead_lettered", err == nil)
	return err == nil
}

// decodeEvent reads an event envelope and reports whether it is a well-formed event of this topic.
func decodeEvent(value []byte, topic string) (Event, bool) {
	var event Event
	if json.Unmarshal(value, &event) != nil {
		return event, false
	}
	return event, ID(event.ID) && event.Type == topic && event.Version == 1 && ID(event.Correlation) && !event.Timestamp.IsZero() && event.Resource != ""
}

// handle runs the handler with retries. It reports whether the event was completed, and whether the handler
// declared it invalid (so it belongs in the dead-letter topic rather than being retried).
func (a *App) handle(ctx context.Context, event Event, handler Handler) (completed, invalid bool) {
	for attempt := 0; attempt < 5 && ctx.Err() == nil; attempt++ {
		err := a.handleOnce(ctx, event, handler)
		if err == nil {
			return true, false
		}
		var bad InvalidEvent
		if errors.As(err, &bad) {
			return false, true
		}
		slog.Error("consumer_retry", "event_id", event.ID, "attempt", attempt+1)
		select {
		case <-ctx.Done():
			return false, false
		case <-time.After(time.Duration(1<<attempt) * time.Second):
		}
	}
	return false, false
}

// handleOnce records the event in the inbox and runs the handler in the same transaction; a duplicate is a no-op.
func (a *App) handleOnce(ctx context.Context, event Event, handler Handler) error {
	workCtx, cancel := context.WithTimeout(context.WithValue(ctx, correlationKey, event.Correlation), 10*time.Second)
	defer cancel()
	tx, err := a.DB.Begin(workCtx)
	if err != nil {
		return err
	}
	defer tx.Rollback(workCtx)
	result, err := tx.Exec(workCtx, "INSERT INTO inbox(event_id) VALUES($1) ON CONFLICT DO NOTHING", event.ID)
	if err != nil {
		return err
	}
	if result.RowsAffected() > 0 {
		if err = handler(workCtx, tx, event); err != nil {
			return err
		}
	}
	return tx.Commit(workCtx)
}
