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

const EventMigration = `
CREATE TABLE IF NOT EXISTS outbox (id uuid PRIMARY KEY, topic text NOT NULL, partition_key text NOT NULL, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz, attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS inbox (event_id uuid PRIMARY KEY, processed_at timestamptz NOT NULL DEFAULT now());`

type Event struct {
	ID          string          `json:"event_id"`
	Type        string          `json:"event_type"`
	Version     int             `json:"schema_version"`
	Timestamp   time.Time       `json:"timestamp"`
	Correlation string          `json:"correlation_id"`
	Resource    string          `json:"resource_id"`
	Data        json.RawMessage `json:"data"`
}

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
	rows, err := tx.Query(batchCtx, `SELECT id,topic,partition_key,body FROM outbox WHERE published_at IS NULL AND attempts<$1 AND next_attempt<=now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT $2`, OutboxMaxAttempts, OutboxBatch)
	if err != nil {
		return 0, err
	}
	batch := []outboxRow{}
	for rows.Next() {
		var row outboxRow
		if err = rows.Scan(&row.id, &row.topic, &row.key, &row.body); err != nil {
			rows.Close()
			return 0, err
		}
		batch = append(batch, row)
	}
	rows.Close()
	if err = rows.Err(); err != nil || len(batch) == 0 {
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
	if len(published) > 0 {
		if _, err = tx.Exec(batchCtx, "UPDATE outbox SET published_at=now() WHERE id=ANY($1::uuid[])", published); err != nil {
			return 0, err
		}
	}
	if len(retry) > 0 {
		slog.Error("outbox_publish_failed", "events", len(retry))
		var abandoned []string
		err = tx.QueryRow(batchCtx, `WITH failed AS (UPDATE outbox SET attempts=attempts+1,next_attempt=now()+make_interval(secs=>LEAST(300,POWER(2,LEAST(attempts,9))::int)) WHERE id=ANY($1::uuid[]) RETURNING id,attempts)
			SELECT coalesce(array_agg(id::text),'{}') FROM failed WHERE attempts>=$2`, retry, OutboxMaxAttempts).Scan(&abandoned)
		if err != nil {
			return 0, err
		}
		for _, id := range abandoned {
			slog.Error("outbox_event_abandoned", "event_id", id, "attempts", OutboxMaxAttempts)
		}
	}
	if err = tx.Commit(batchCtx); err != nil {
		return 0, err
	}
	if len(retry) > 0 {
		return 0, nil
	}
	return len(batch), nil
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

type InvalidEvent struct{ Reason string }

func (e InvalidEvent) Error() string { return e.Reason }

// Consume processes a topic for as long as ctx lives. A session stops without committing when an event keeps
// failing (the database is down, say); Consume then waits, backing off to a minute, and starts a new session from
// the last committed offset, so the event is retried rather than skipped and the consumer never stays down.
// healthy, if given, is true while a session runs and false while it waits to restart.
func (a *App) Consume(ctx context.Context, topic string, handler func(context.Context, pgx.Tx, Event) error, healthy *atomic.Bool) {
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
func (a *App) consumeSession(ctx context.Context, topic string, handler func(context.Context, pgx.Tx, Event) error) bool {
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
		var event Event
		err = json.Unmarshal(msg.Value, &event)
		poison := err != nil || !ID(event.ID) || event.Type != topic || event.Version != 1 || !ID(event.Correlation) || event.Timestamp.IsZero() || event.Resource == ""
		completed := false
		for attempt := 0; !poison && attempt < 5 && ctx.Err() == nil; attempt++ {
			workCtx, cancel := context.WithTimeout(context.WithValue(ctx, correlationKey, event.Correlation), 10*time.Second)
			tx, txErr := a.DB.Begin(workCtx)
			if txErr == nil {
				result, insertErr := tx.Exec(workCtx, "INSERT INTO inbox(event_id) VALUES($1) ON CONFLICT DO NOTHING", event.ID)
				txErr = insertErr
				if txErr == nil && result.RowsAffected() > 0 {
					txErr = handler(workCtx, tx, event)
				}
				if txErr == nil {
					txErr = tx.Commit(workCtx)
				} else {
					_ = tx.Rollback(workCtx)
				}
			}
			cancel()
			if txErr == nil {
				completed = true
				break
			}
			var invalid InvalidEvent
			if errors.As(txErr, &invalid) {
				poison = true
				break
			}
			slog.Error("consumer_retry", "event_id", event.ID, "attempt", attempt+1)
			select {
			case <-ctx.Done():
				return progressed
			case <-time.After(time.Duration(1<<attempt) * time.Second):
			}
		}
		if poison {
			err = dlq.WriteMessages(ctx, kafka.Message{Topic: topic + ".dlq", Key: msg.Key, Value: msg.Value})
			completed = err == nil
			slog.Error("invalid_event", "event_id", event.ID, "dead_lettered", completed)
		}
		if !completed {
			slog.Error("consumer_stopped_uncommitted", "topic", topic, "event_id", event.ID)
			return progressed
		}
		if err = reader.CommitMessages(ctx, msg); err != nil {
			slog.Error("consumer_commit_failed", "event_id", event.ID)
			return progressed
		}
		progressed = true
	}
	return progressed
}
